"""Serveur MCP (Streamable HTTP) exposant les outils au harnais : `connect_session`,
`system_info`, `run_command`, `run_shell` (MVP), `terminate_session` (kill-switch,
phase 5) et `issue_client_token` (émission de jeton client `per_session`, phase 5).

Utilise le **SDK MCP officiel** (`mcp.server.fastmcp.FastMCP`) pour la
définition des outils et le transport Streamable HTTP — voir
`FastMCP.streamable_http_app()`. Cette couche ne connaît rien du réseau
client↔relay : elle appelle uniquement `broker.dispatch_command(...)` /
`broker.get_session_info(...)` / `broker.terminate_session(...)` (voir
`broker.py`) et traduit les chunks `stream`/`result` agrégés (ou les
exceptions du protocole, dont `CommandDeniedError` — refus par
`CommandPolicy`, phase 5) en un dict de résultat structuré pour l'outil.

## Auth MCP : `MCP_AUTH_MODE=static_bearer` (défaut) vs `oauth`

Deux modes, sélectionnés par l'appelant (`relay/app.py`) via
`build_mcp_asgi_app(broker, mode=...)` :

- `static_bearer` (défaut, compat MVP inchangée) : Bearer pré-partagé
  (`MCP_BEARER_TOKEN`) via un middleware ASGI minimal
  (:class:`BearerAuthMiddleware`), sans notion de scope — tous les outils
  sont accessibles à quiconque présente le jeton. Volontairement séparé des
  primitives OAuth du SDK MCP : un jeton statique unique ne bénéficierait de
  rien à passer par `token_verifier`/`AuthSettings`.

- `oauth` : **Resource Server OAuth 2.1** utilisant réellement les primitives
  `mcp.server.auth` du SDK — `FastMCP(token_verifier=JWTTokenVerifier(...),
  auth=AuthSettings(...))`. À la construction de `streamable_http_app()`, le
  SDK câble lui-même `BearerAuthBackend` (valide le jeton via
  `JWTTokenVerifier.verify_token`, cf. `relay/jwt_auth.py`) +
  `AuthContextMiddleware` (expose le jeton validé via
  `mcp.server.auth.middleware.auth_context.get_access_token()` pendant
  l'exécution de l'outil) + `RequireAuthMiddleware` (401 si aucun jeton
  valide). C'est le point d'entrée officiel du SDK, pas un middleware maison.

  **Compromis assumé** : `AuthSettings.required_scopes` n'exprime qu'un
  ensemble de scopes *global* à tout l'endpoint MCP, alors que nos outils
  demandent des scopes *différents* (`connect_session` ≠ `run_command` ≠
  `terminate_session` ≠ `issue_client_token`). On configure donc
  `required_scopes=[]` au niveau transport (« un jeton valide et non expiré
  suffit pour entrer ») et l'enforcement **par outil** est fait ici, dans
  chaque fonction d'outil, via `get_access_token()` (voir `_check_scope` /
  `TOOL_SCOPES` ci-dessous) — c'est la seule partie qui n'est pas déléguée
  telle quelle au SDK, faute d'un mécanisme SDK par-outil pour l'exprimer.
  Un jeton sans le scope requis reçoit une erreur d'outil claire
  (`{"status": "error", "error": "forbidden_scope", ...}`) et l'événement est
  journalisé dans l'audit (`decision: "denied"`) si un `audit_log` est fourni.

`require_scopes=False` (défaut de `create_mcp_server`) désactive totalement
cet enforcement par outil, pour ne rien changer au comportement historique
(tests existants qui appellent les outils directement sans aucun contexte
d'authentification).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import PerSessionTokenStore, extract_bearer_token, verify_token
from .broker import (
    Broker,
    ClientDisconnectedError,
    CommandDeniedError,
    CommandTimeoutError,
    SessionNotFoundError,
)
from .jwt_auth import DEFAULT_ALGORITHM, JWTTokenVerifier

if TYPE_CHECKING:
    from .audit import AuditLog

SERVER_NAME = "claude-distant-relay"

DEFAULT_CLIENT_TOKEN_TTL_SECONDS = 1800.0
DEFAULT_RESOURCE_SERVER_URL = "https://claude-distant.local/mcp"
DEFAULT_ISSUER_URL = "https://claude-distant.local/"

# Scope MCP requis par outil, appliqué uniquement quand `require_scopes=True`
# (mode oauth) — voir docstring de module.
SCOPE_SESSION_CONNECT = "session:connect"
SCOPE_COMMAND_EXECUTE = "command:execute"
SCOPE_SESSION_TERMINATE = "session:terminate"
SCOPE_CLIENT_PROVISION = "client:provision"

TOOL_SCOPES: dict[str, str] = {
    "connect_session": SCOPE_SESSION_CONNECT,
    "run_command": SCOPE_COMMAND_EXECUTE,
    "run_shell": SCOPE_COMMAND_EXECUTE,
    "terminate_session": SCOPE_SESSION_TERMINATE,
    "issue_client_token": SCOPE_CLIENT_PROVISION,
}


def _check_scope(
    require_scopes: bool,
    tool: str,
    required_scope: str,
    audit_log: "AuditLog | None",
    session_code: str | None = None,
) -> dict[str, Any] | None:
    """Retourne un dict d'erreur d'outil si `required_scope` manque, sinon `None`.

    No-op (retourne toujours `None`) si `require_scopes` est `False` — c'est
    ce qui préserve le comportement `static_bearer`/MVP historique (aucune
    notion de scope). En mode oauth, lit le jeton validé par le SDK via
    `get_access_token()` (contextvar posé par `AuthContextMiddleware` pour une
    vraie requête HTTP, ou positionné directement dans les tests unitaires).
    """
    if not require_scopes:
        return None

    access_token = get_access_token()
    if access_token is not None and required_scope in (access_token.scopes or []):
        return None

    if audit_log is not None:
        audit_log.record(
            {
                "session_code": session_code,
                "tool": tool,
                "decision": "denied",
                "outcome": {"reason": f"missing_scope:{required_scope}"},
            }
        )
    return {
        "status": "error",
        "error": "forbidden_scope",
        "detail": f"le jeton ne porte pas le scope requis : {required_scope!r}",
    }


def create_mcp_server(
    broker: Broker,
    *,
    require_scopes: bool = False,
    client_token_store: "PerSessionTokenStore | None" = None,
    client_token_ttl_seconds: float = DEFAULT_CLIENT_TOKEN_TTL_SECONDS,
    audit_log: "AuditLog | None" = None,
    token_verifier: "TokenVerifier | None" = None,
    auth_settings: "AuthSettings | None" = None,
) -> FastMCP:
    """Construit un `FastMCP` et y enregistre les outils, branchés sur
    `broker.dispatch_command`.

    `broker` doit exposer `get_session_info(session_code)`,
    `dispatch_command(session_code, tool, params, timeout)` et
    `terminate_session(session_code)` (voir `relay.broker.Broker` ; un double
    de test compatible suffit).

    Tous les paramètres après `broker` sont optionnels et à valeur par défaut
    neutre : un appel `create_mcp_server(broker)` reproduit exactement le
    comportement MVP (aucun scope, pas d'`issue_client_token` fonctionnel sans
    store, pas d'audit des refus). `token_verifier`/`auth_settings` sont
    transmis tels quels au constructeur `FastMCP` (mode oauth, voir
    `build_mcp_asgi_app`) ; `host="0.0.0.0"` est fixé explicitement pour
    éviter que `FastMCP` n'active silencieusement sa protection anti DNS
    rebinding restreinte à `localhost` (son défaut quand `host` n'est pas
    précisé) — inadaptée à un relay conçu pour tourner derrière un reverse
    proxy TLS avec un nom d'hôte public (voir `docs/SECURITY.md`).
    """
    mcp = FastMCP(
        name=SERVER_NAME,
        stateless_http=True,
        host="0.0.0.0",
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    @mcp.tool()
    async def connect_session(session_code: str) -> dict[str, Any]:
        """Vérifie qu'une session est active et retourne son OS/hostname/version."""
        scope_error = _check_scope(
            require_scopes, "connect_session", SCOPE_SESSION_CONNECT, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
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
        scope_error = _check_scope(
            require_scopes, "run_command", SCOPE_COMMAND_EXECUTE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
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
        scope_error = _check_scope(
            require_scopes, "run_shell", SCOPE_COMMAND_EXECUTE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        return await _dispatch_and_aggregate(
            broker,
            session_code,
            "run_shell",
            {"command": command, "shell": shell},
            timeout=timeout,
        )

    @mcp.tool()
    async def terminate_session(session_code: str) -> dict[str, Any]:
        """Kill-switch : invalide immédiatement une session active.

        Ferme/notifie la connexion client WS et fait échouer proprement toute
        commande en cours pour cette session (cf. `Broker.terminate_session`).
        """
        scope_error = _check_scope(
            require_scopes, "terminate_session", SCOPE_SESSION_TERMINATE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        terminated = await broker.terminate_session(session_code)
        if not terminated:
            return {"status": "not_found", "session_code": session_code}
        return {"status": "terminated", "session_code": session_code}

    @mcp.tool()
    async def issue_client_token(ttl_seconds: float | None = None) -> dict[str, Any]:
        """Émet un jeton client court à usage unique (mode `CLIENT_AUTH_MODE=per_session`).

        Le jeton retourné doit être transmis à l'opérateur/PC distant pour
        authentifier la prochaine connexion WS
        (`Authorization: Bearer <jeton>`) ; il est consommé dès le premier
        `register` réussi (cf. `relay/auth.py:PerSessionTokenStore`). Protégé
        par le scope `client:provision` en mode oauth. Remplace l'émission
        manuelle par appel direct à `PerSessionTokenStore.issue(...)` côté
        opérateur/déploiement (TODO historique de la vague précédente).
        """
        scope_error = _check_scope(require_scopes, "issue_client_token", SCOPE_CLIENT_PROVISION, audit_log)
        if scope_error is not None:
            return scope_error
        if client_token_store is None:
            return {
                "status": "error",
                "error": "not_configured",
                "detail": "aucun PerSessionTokenStore configuré sur ce relay",
            }
        ttl = ttl_seconds if ttl_seconds is not None else client_token_ttl_seconds
        token = client_token_store.issue(ttl_seconds=ttl)
        return {"status": "ok", "token": token, "expires_in": ttl}

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
    except CommandDeniedError as exc:
        return {"status": "error", "error": "denied", "detail": str(exc)}

    return {
        "status": "ok",
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        **outcome,
    }


class BearerAuthMiddleware:
    """Middleware ASGI minimal : exige `Authorization: Bearer <MCP_BEARER_TOKEN>`.

    Utilisé uniquement en mode `static_bearer` (voir docstring de module) ;
    ne s'applique qu'aux requêtes HTTP (laisse passer les autres types de
    scope, ex. `lifespan`, tels quels).
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


def build_mcp_asgi_app(
    broker: Broker,
    *,
    mode: str = "static_bearer",
    bearer_token: str | None = None,
    jwt_secret: str | None = None,
    jwt_algorithm: str = DEFAULT_ALGORITHM,
    resource_server_url: str | None = None,
    issuer_url: str | None = None,
    client_token_store: "PerSessionTokenStore | None" = None,
    client_token_ttl_seconds: float = DEFAULT_CLIENT_TOKEN_TTL_SECONDS,
    audit_log: "AuditLog | None" = None,
) -> tuple[FastMCP, ASGIApp]:
    """Construit le serveur MCP et son app ASGI, selon `mode` (`MCP_AUTH_MODE`).

    Retourne `(mcp, asgi_app)` : `mcp` est nécessaire à l'appelant (`app.py`)
    pour piloter le cycle de vie de `mcp.session_manager` (voir docstring de
    `app.py` sur le montage lifespan d'une sous-app Starlette dans FastAPI).

    - `mode="static_bearer"` (défaut) : comportement MVP inchangé — un unique
      `MCP_BEARER_TOKEN` protège l'endpoint via :class:`BearerAuthMiddleware`,
      aucun enforcement de scope (`require_scopes=False`).
    - `mode="oauth"` : câble le **vrai** SDK MCP (`token_verifier`/`auth`, cf.
      docstring de module) avec un :class:`~relay.jwt_auth.JWTTokenVerifier`
      HS256 ; l'app ASGI retournée est directement `mcp.streamable_http_app()`
      (déjà protégée par `BearerAuthBackend`/`RequireAuthMiddleware` côté
      SDK), sans couche `BearerAuthMiddleware` supplémentaire.

    Dans les deux modes, `client_token_store`/`audit_log` sont transmis à
    `create_mcp_server` pour activer `issue_client_token` / l'audit des refus
    de scope.
    """
    if mode == "oauth":
        verifier = JWTTokenVerifier(secret=jwt_secret or "", algorithm=jwt_algorithm)
        auth_settings = AuthSettings(
            issuer_url=issuer_url or DEFAULT_ISSUER_URL,
            resource_server_url=resource_server_url or DEFAULT_RESOURCE_SERVER_URL,
            required_scopes=[],
        )
        mcp = create_mcp_server(
            broker,
            require_scopes=True,
            client_token_store=client_token_store,
            client_token_ttl_seconds=client_token_ttl_seconds,
            audit_log=audit_log,
            token_verifier=verifier,
            auth_settings=auth_settings,
        )
        return mcp, mcp.streamable_http_app()

    # mode == "static_bearer" (défaut, comportement MVP inchangé)
    mcp = create_mcp_server(
        broker,
        client_token_store=client_token_store,
        client_token_ttl_seconds=client_token_ttl_seconds,
        audit_log=audit_log,
    )
    inner_app = mcp.streamable_http_app()
    return mcp, BearerAuthMiddleware(inner_app, bearer_token or "")
