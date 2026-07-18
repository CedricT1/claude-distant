"""Broker WebSocket : gère les connexions client, le routage des commandes et
l'agrégation `stream`/`result` corrélée par `request_id`.

Ce module est agnostique du transport réseau : il attend un objet
« connexion » exposant une méthode async `send_json(message: dict)`. La
couche `app.py` fournit un adaptateur autour de la WebSocket FastAPI réelle ;
les tests unitaires utilisent une connexion factice pour éviter tout réseau.

API interne exposée à la couche MCP (`mcp_server.py`) :
`dispatch_command(session_code, tool, params, timeout) -> async generator`.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from .session_store import SessionStore

DEFAULT_TTL_SECONDS = 1800
DEFAULT_COMMAND_TIMEOUT = 60


class SessionNotFoundError(Exception):
    """Le code de session est inconnu ou a expiré (TTL dépassé)."""


class ClientDisconnectedError(Exception):
    """Le client s'est déconnecté avant ou pendant l'exécution de la commande."""


class CommandTimeoutError(Exception):
    """Aucune réponse (`stream`/`result`) reçue du client dans le délai imparti."""


class ConnectionLike(Protocol):
    """Interface minimale requise d'une connexion client par le broker."""

    async def send_json(self, message: dict[str, Any]) -> None: ...


_DISCONNECTED = object()  # sentinelle pushée dans les files en attente à la déconnexion


@dataclass
class _PendingRequest:
    connection: Any
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)


class Broker:
    """Gère le cycle de vie des connexions client et le routage des commandes."""

    def __init__(
        self,
        session_store: SessionStore,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self._session_store = session_store
        self._default_ttl_seconds = default_ttl_seconds
        self._default_command_timeout = command_timeout
        self._pending: dict[str, _PendingRequest] = {}

    # -- Cycle de vie de la connexion -------------------------------------

    async def register_connection(
        self, connection: ConnectionLike, os: str, hostname: str, version: str
    ) -> str:
        """Enregistre une connexion client fraîchement `register`-ée et retourne son code."""
        return await self._session_store.create(
            connection=connection,
            os=os,
            hostname=hostname,
            version=version,
            ttl_seconds=self._default_ttl_seconds,
        )

    async def heartbeat(self, session_code: str) -> bool:
        """Prolonge le TTL d'une session sur réception d'un `heartbeat` client."""
        return await self._session_store.touch(session_code, self._default_ttl_seconds)

    async def get_session_info(self, session_code: str):
        """Retourne le `SessionRecord` du code, ou `None` si inconnu/expiré.

        Utilisé par l'outil MCP `connect_session` pour rapporter l'OS/hostname
        de la cible sans déclencher de commande.
        """
        return await self._session_store.get(session_code)

    async def unregister_connection(self, connection: ConnectionLike) -> None:
        """À appeler quand la WebSocket client se ferme (propre ou non).

        Supprime la session du store et réveille immédiatement toute commande
        en cours pour cette connexion avec :class:`ClientDisconnectedError`,
        plutôt que d'attendre le timeout.
        """
        await self._session_store.remove_by_connection(connection)
        for pending in list(self._pending.values()):
            if pending.connection is connection:
                pending.queue.put_nowait(_DISCONNECTED)

    # -- Réception des messages client -------------------------------------

    async def handle_client_message(self, connection: ConnectionLike, message: dict[str, Any]) -> None:
        """Route un message reçu du client vers la requête en attente correspondante.

        Gère `stream`, `result` et `approval_response` (tous portent
        `request_id`). Les autres types (`register`, `heartbeat`) sont gérés
        en amont par la boucle WS de `app.py`.
        """
        msg_type = message.get("type")
        request_id = message.get("request_id")
        if request_id is None:
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.connection is not connection:
            return  # requête inconnue, déjà terminée, ou usurpation d'une autre connexion

        if msg_type == "stream":
            chunk = {"type": "stream", "stream": message.get("stream"), "data": message.get("data")}
            await pending.queue.put((chunk, False))
        elif msg_type == "result":
            chunk = {
                "type": "result",
                "exit_code": message.get("exit_code"),
                "error": message.get("error"),
            }
            await pending.queue.put((chunk, True))
        elif msg_type == "approval_response":
            chunk = {"type": "approval_response", "approved": message.get("approved")}
            await pending.queue.put((chunk, True))

    # -- API interne pour la couche MCP ------------------------------------

    async def dispatch_command(
        self,
        session_code: str,
        tool: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Envoie une commande au client ciblé et streame les chunks agrégés.

        Yield des dicts `{"type": "stream", ...}` puis un dernier
        `{"type": "result", ...}` (ou `approval_response` si la commande a été
        refusée localement). Lève :class:`SessionNotFoundError`,
        :class:`ClientDisconnectedError` ou :class:`CommandTimeoutError`.
        """
        record = await self._session_store.get(session_code)
        if record is None:
            raise SessionNotFoundError(f"session inconnue ou expirée : {session_code}")

        connection = record.connection
        request_id = uuid.uuid4().hex
        pending = _PendingRequest(connection=connection)
        self._pending[request_id] = pending
        effective_timeout = timeout if timeout is not None else self._default_command_timeout

        try:
            try:
                await connection.send_json(
                    {
                        "type": "command",
                        "request_id": request_id,
                        "tool": tool,
                        "params": params,
                    }
                )
            except Exception as exc:
                raise ClientDisconnectedError(
                    f"impossible d'envoyer la commande au client : {exc}"
                ) from exc

            while True:
                try:
                    item = await asyncio.wait_for(pending.queue.get(), timeout=effective_timeout)
                except asyncio.TimeoutError as exc:
                    raise CommandTimeoutError(
                        f"timeout en attente de la réponse à la commande {request_id}"
                    ) from exc

                if item is _DISCONNECTED:
                    raise ClientDisconnectedError(
                        "client déconnecté pendant l'exécution de la commande"
                    )

                chunk, final = item
                yield chunk
                if final:
                    return
        finally:
            self._pending.pop(request_id, None)
