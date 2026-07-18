"""Application FastAPI du relay : broker WebSocket (`/ws/client`), serveur MCP
Streamable HTTP (`/mcp`) et `/healthz`. Point d'entrée uvicorn : `relay.app:app`.

Variables d'environnement :
  - `CLIENT_TOKEN` : jeton Bearer attendu du client PC distant sur `/ws/client`
    (mode `CLIENT_AUTH_MODE=shared`, défaut).
  - `MCP_BEARER_TOKEN` : jeton Bearer attendu du harnais sur `/mcp`.
  - `SESSION_TTL_SECONDS` : TTL par défaut d'un code de session (défaut 1800).
  - `CLIENT_AUTH_MODE` : `shared` (défaut, jeton unique pré-partagé) ou
    `per_session` (jeton court à usage unique, TTL = TTL de session — voir
    `relay/auth.py` pour le mécanisme complet et son TODO OAuth).
  - `COMMAND_DENYLIST` / `COMMAND_ALLOWLIST` / `MAX_COMMANDS_PER_SESSION` /
    `RATE_LIMIT_PER_MINUTE` : politique de commandes (voir `relay/command_policy.py`).
  - `AUDIT_LOG_PATH` : chemin du journal d'audit JSONL chaîné (voir `relay/audit.py`).
  - `HOST` / `PORT` : interface d'écoute uvicorn (défaut 0.0.0.0:8000).

Si `CLIENT_TOKEN`/`MCP_BEARER_TOKEN` ne sont pas définis, l'app démarre quand
même (pas de crash à l'import) mais `auth.verify_token` refuse alors *tout*
le monde par construction (un jeton attendu vide ne matche jamais) : le relay
est sûr par défaut plutôt que de s'ouvrir sans authentification.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .audit import AuditLog
from .auth import PerSessionTokenStore, extract_bearer_token, verify_client_token
from .broker import Broker
from .command_policy import CommandPolicy
from .mcp_server import build_mcp_asgi_app
from .session_store import InMemorySessionStore

DEFAULT_SESSION_TTL_SECONDS = 1800
DEFAULT_CLIENT_AUTH_MODE = "shared"
WS_AUTH_FAILED_CLOSE_CODE = 4401  # code custom (plage 4000-4999), miroir du 401 HTTP
WS_SESSION_TERMINATED_CLOSE_CODE = 4402  # kill-switch : session invalidée côté harnais


class _WebSocketConnection:
    """Adapte la WebSocket FastAPI réelle à l'interface `ConnectionLike` du broker."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_json(self, message: dict[str, Any]) -> None:
        await self._websocket.send_json(message)

    async def close(self) -> None:
        """Ferme la connexion WS sous-jacente (utilisé par le kill-switch, cf. Broker.terminate_session)."""
        await self._websocket.close(code=WS_SESSION_TERMINATED_CLOSE_CODE)


def create_app(
    client_token: str,
    mcp_bearer_token: str,
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    client_auth_mode: str = DEFAULT_CLIENT_AUTH_MODE,
    command_policy: CommandPolicy | None = None,
    audit_log: AuditLog | None = None,
) -> FastAPI:
    """Construit une instance FastAPI complète et isolée (broker + MCP + healthz).

    Une factory (plutôt qu'un unique singleton module-level) permet aux tests
    de créer des apps indépendantes avec leurs propres tokens/store, sans
    dépendre des variables d'environnement du process.

    `client_auth_mode` sélectionne le mode d'authentification du canal
    `/ws/client` (`shared` par défaut, compat MVP ; `per_session` pour des
    jetons courts à usage unique — voir `relay/auth.py`). `command_policy`/
    `audit_log` sont optionnels et branchés sur le `Broker` (voir
    `relay/command_policy.py` / `relay/audit.py`) ; laissés à `None`, aucune
    restriction ni journalisation n'est appliquée (comportement MVP inchangé).
    """
    session_store = InMemorySessionStore()
    broker = Broker(
        session_store=session_store,
        default_ttl_seconds=session_ttl_seconds,
        command_policy=command_policy,
        audit_log=audit_log,
    )
    mcp, mcp_asgi_app = build_mcp_asgi_app(broker, mcp_bearer_token)
    client_token_store = PerSessionTokenStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Le serveur MCP Streamable HTTP est monté comme sous-app ASGI ; son
        # StreamableHTTPSessionManager doit tourner dans le lifespan de l'app
        # *parente* car Starlette ne route le scope "lifespan" vers aucune
        # sous-app montée via `app.mount(...)` (seuls les scopes "http" et
        # "websocket" sont dispatchés aux `Mount`) : sans ça, le session
        # manager ne serait jamais démarré. C'est le pattern documenté par le
        # SDK MCP pour « mounting multiple FastMCP servers in a single FastAPI
        # application ».
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="claude-distant relay", lifespan=lifespan)
    app.state.broker = broker
    app.state.session_store = session_store
    app.state.mcp = mcp
    app.state.client_token_store = client_token_store

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws/client")
    async def ws_client(websocket: WebSocket) -> None:
        """Point d'entrée du client PC distant (connexion sortante, cf. docs/PROTOCOL.md).

        L'authentification dépend de `client_auth_mode` (voir docstring de
        `create_app` et de `relay/auth.py`) : `shared` compare au
        `CLIENT_TOKEN` pré-partagé (comportement MVP inchangé), `per_session`
        valide contre `client_token_store` (jeton court à usage unique).
        """
        auth_header = websocket.headers.get("authorization")
        token = extract_bearer_token(auth_header)
        if not verify_client_token(token, client_auth_mode, client_token, client_token_store):
            # Fermer avant d'accepter : uvicorn traduit ceci en rejet du
            # handshake WS (HTTP 403) plutôt que d'ouvrir puis fermer la
            # connexion — le client ne peut jamais envoyer de message.
            await websocket.close(code=WS_AUTH_FAILED_CLOSE_CODE)
            return

        await websocket.accept()
        connection = _WebSocketConnection(websocket)
        session_code: str | None = None
        try:
            while True:
                message = await websocket.receive_json()
                msg_type = message.get("type")
                if msg_type == "register":
                    if client_auth_mode == "per_session" and token is not None:
                        # Jeton à usage unique : consommé dès le premier
                        # register réussi, pour empêcher toute réutilisation.
                        client_token_store.consume(token)
                    session_code = await broker.register_connection(
                        connection,
                        os=message.get("os", "unknown"),
                        hostname=message.get("hostname", ""),
                        version=message.get("version", ""),
                    )
                    await websocket.send_json({"type": "registered", "session_code": session_code})
                elif msg_type == "heartbeat":
                    if session_code is not None:
                        await broker.heartbeat(session_code)
                    await websocket.send_json({"type": "heartbeat_ack"})
                elif msg_type in ("stream", "result", "approval_response"):
                    await broker.handle_client_message(connection, message)
                # types inconnus : ignorés silencieusement (extension tolérante du protocole)
        except WebSocketDisconnect:
            pass
        finally:
            await broker.unregister_connection(connection)

    # Monté en dernier : les routes explicites ci-dessus (/healthz, /ws/client)
    # sont enregistrées avant et donc prioritaires dans le routage Starlette.
    app.mount("/mcp", mcp_asgi_app)

    return app


CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN", "")
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
SESSION_TTL_SECONDS = float(os.environ.get("SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS)))
CLIENT_AUTH_MODE = os.environ.get("CLIENT_AUTH_MODE", DEFAULT_CLIENT_AUTH_MODE)
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Point d'entrée pour `uvicorn relay.app:app`. `CommandPolicy.from_env()` et
# `AuditLog()` lisent leurs propres variables d'environnement (voir
# `relay/command_policy.py` / `relay/audit.py`) ; ce module réel les instancie
# systématiquement, contrairement à `create_app(...)` appelé directement par
# les tests (qui laisse `command_policy`/`audit_log` à `None` par défaut).
app = create_app(
    client_token=CLIENT_TOKEN,
    mcp_bearer_token=MCP_BEARER_TOKEN,
    session_ttl_seconds=SESSION_TTL_SECONDS,
    client_auth_mode=CLIENT_AUTH_MODE,
    command_policy=CommandPolicy.from_env(),
    audit_log=AuditLog(),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
