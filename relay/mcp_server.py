"""Serveur MCP (Streamable HTTP) exposant les 4 outils MVP au harnais.

Utilise le **SDK MCP officiel** (`mcp.server.fastmcp.FastMCP`) pour la
définition des outils et le transport Streamable HTTP — voir
`FastMCP.streamable_http_app()`. Cette couche ne connaît rien du réseau
client↔relay : elle appelle uniquement `broker.dispatch_command(...)` /
`broker.get_session_info(...)` (voir `broker.py`) et traduit les
chunks `stream`/`result` agrégés en un dict de résultat structuré pour
l'outil.

## Auth Bearer

Le MVP protège l'endpoint MCP avec un Bearer pré-partagé (`MCP_BEARER_TOKEN`)
via un middleware ASGI minimal (:class:`BearerAuthMiddleware`), plutôt que
les primitives `token_verifier`/`AuthSettings` du SDK. Ces dernières sont
conçues pour un vrai serveur d'autorisation OAuth (avec `resource_server_url`,
métadonnées `.well-known/oauth-protected-resource`, scopes, etc.) — trop
lourd pour un simple jeton statique de MVP, mais c'est le point d'entrée
officiel prévu pour la suite.

TODO (phase 5 du plan — durcissement / OAuth 2.1) :
  - remplacer `BearerAuthMiddleware` par `mcp.server.auth` :
    `FastMCP(auth=AuthSettings(...), token_verifier=MonTokenVerifier())` où
    `MonTokenVerifier` implémente `mcp.server.auth.provider.TokenVerifier`
    (`async def verify_token(self, token: str) -> AccessToken | None`).
  - jetons de session courts par session (au lieu d'un jeton MCP unique).
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import extract_bearer_token, verify_token
from .broker import Broker, ClientDisconnectedError, CommandTimeoutError, SessionNotFoundError

SERVER_NAME = "claude-distant-relay"


def create_mcp_server(broker: Broker) -> FastMCP:
    """Construit un `FastMCP` et y enregistre `connect_session`, `system_info`,
    `run_command`, `run_shell`, branchés sur `broker.dispatch_command`.

    `broker` doit exposer `get_session_info(session_code)` et
    `dispatch_command(session_code, tool, params, timeout)` (voir
    `relay.broker.Broker` ; un double de test compatible suffit).
    """
    mcp = FastMCP(name=SERVER_NAME, stateless_http=True)

    @mcp.tool()
    async def connect_session(session_code: str) -> dict[str, Any]:
        """Vérifie qu'une session est active et retourne son OS/hostname/version."""
        record = await broker.get_session_info(session_code)
        if record is None:
            return {"status": "not_found", "session_code": session_code}
        return {
            "status": "connected",
            "session_code": session_code,
            "os": record.os,
            "hostname": record.hostname,
            "version": record.version,
        }

    @mcp.tool()
    async def system_info(session_code: str) -> dict[str, Any]:
        """Retourne les infos système (OS, uptime, RAM, CPU) de la cible."""
        return await _dispatch_and_aggregate(broker, session_code, "system_info", {})

    @mcp.tool()
    async def run_command(
        session_code: str, command: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Exécute une commande simple sur la cible (stdout/stderr/exit_code)."""
        return await _dispatch_and_aggregate(
            broker, session_code, "run_command", {"command": command}, timeout=timeout
        )

    @mcp.tool()
    async def run_shell(
        session_code: str,
        command: str,
        shell: str = "auto",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Exécute une commande dans le shell natif de la cible.

        `shell="auto"` (défaut) : PowerShell sur Windows, Bash sur Linux,
        selon l'OS détecté à `register`. Override possible :
        `powershell`/`pwsh`/`bash`/`sh`.
        """
        return await _dispatch_and_aggregate(
            broker,
            session_code,
            "run_shell",
            {"command": command, "shell": shell},
            timeout=timeout,
        )

    return mcp


async def _dispatch_and_aggregate(
    broker: Broker,
    session_code: str,
    tool: str,
    params: dict[str, Any],
    timeout: float | None = None,
) -> dict[str, Any]:
    """Consomme `broker.dispatch_command(...)` et agrège en un résultat unique.

    Concatène les chunks `stream` par flux (stdout/stderr) et rapporte le
    `result` final. Traduit les erreurs du protocole (session inconnue,
    client déconnecté, timeout) en un dict `{"status": "error", ...}`
    exploitable par le harnais, sans jamais laisser fuiter une exception
    Python côté MCP.
    """
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    outcome: dict[str, Any] = {}

    try:
        async for chunk in broker.dispatch_command(session_code, tool, params, timeout=timeout):
            chunk_type = chunk.get("type")
            if chunk_type == "stream":
                if chunk.get("stream") == "stderr":
                    stderr_parts.append(chunk.get("data") or "")
                else:
                    stdout_parts.append(chunk.get("data") or "")
            elif chunk_type == "result":
                outcome = {"exit_code": chunk.get("exit_code"), "error": chunk.get("error")}
            elif chunk_type == "approval_response":
                approved = chunk.get("approved")
                outcome = {
                    "exit_code": None,
                    "error": None if approved else "refused_by_user",
                }
    except SessionNotFoundError as exc:
        return {"status": "error", "error": "session_not_found", "detail": str(exc)}
    except ClientDisconnectedError as exc:
        return {"status": "error", "error": "client_disconnected", "detail": str(exc)}
    except CommandTimeoutError as exc:
        return {"status": "error", "error": "timeout", "detail": str(exc)}

    return {
        "status": "ok",
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        **outcome,
    }


class BearerAuthMiddleware:
    """Middleware ASGI minimal : exige `Authorization: Bearer <MCP_BEARER_TOKEN>`.

    Volontairement séparé des primitives OAuth du SDK MCP (voir TODO en tête
    de module) pour garder le MVP simple ; ne s'applique qu'aux requêtes HTTP
    (laisse passer les autres types de scope, ex. `lifespan`, tels quels).
    """

    def __init__(self, app: ASGIApp, expected_token: str) -> None:
        self._app = app
        self._expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_auth = headers.get(b"authorization", b"").decode("latin-1")
        token = extract_bearer_token(raw_auth)
        if not verify_token(token, self._expected_token):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def build_mcp_asgi_app(broker: Broker, bearer_token: str) -> tuple[FastMCP, ASGIApp]:
    """Construit le serveur MCP et son app ASGI protégée par Bearer.

    Retourne `(mcp, asgi_app)` : `mcp` est nécessaire à l'appelant (`app.py`)
    pour piloter le cycle de vie de `mcp.session_manager` (voir docstring de
    `app.py` sur le montage lifespan d'une sous-app Starlette dans FastAPI).
    """
    mcp = create_mcp_server(broker)
    inner_app = mcp.streamable_http_app()
    return mcp, BearerAuthMiddleware(inner_app, bearer_token)
