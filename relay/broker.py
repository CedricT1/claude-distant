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
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol

from .session_store import SessionStore

if TYPE_CHECKING:
    from .audit import AuditLog
    from .command_policy import CommandPolicy

DEFAULT_TTL_SECONDS = 1800
DEFAULT_COMMAND_TIMEOUT = 60


class SessionNotFoundError(Exception):
    """Le code de session est inconnu ou a expiré (TTL dépassé)."""


class ClientDisconnectedError(Exception):
    """Le client s'est déconnecté avant ou pendant l'exécution de la commande."""


class CommandTimeoutError(Exception):
    """Aucune réponse (`stream`/`result`) reçue du client dans le délai imparti."""


class CommandDeniedError(Exception):
    """La commande a été refusée par la `CommandPolicy` (denylist/allowlist/quota)."""


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
        command_policy: "CommandPolicy | None" = None,
        audit_log: "AuditLog | None" = None,
    ) -> None:
        self._session_store = session_store
        self._default_ttl_seconds = default_ttl_seconds
        self._default_command_timeout = command_timeout
        self._pending: dict[str, _PendingRequest] = {}
        # `command_policy`/`audit_log` sont optionnels (défaut `None`) pour
        # rester compatibles avec les usages existants du MVP qui ne les
        # fournissent pas : dans ce cas aucune restriction n'est appliquée et
        # rien n'est journalisé (cf. docs/PLAN.md Phase 5).
        self._command_policy = command_policy
        self._audit_log = audit_log

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

    async def terminate_session(self, session_code: str) -> bool:
        """Kill-switch : invalide immédiatement une session active.

        Marque la session comme invalide dans le store (`SessionStore.terminate`),
        fait échouer proprement toute commande en cours pour cette session
        (avec :class:`ClientDisconnectedError`, comme une déconnexion) puis
        notifie/ferme la connexion client WS sous-jacente (best-effort : si la
        connexion est déjà morte, l'échec est ignoré). Retourne `False` si le
        code de session était déjà inconnu/expiré, `True` sinon.
        """
        record = await self._session_store.terminate(session_code)
        if self._audit_log is not None:
            self._audit_log.record(
                {
                    "session_code": session_code,
                    "tool": "terminate_session",
                    "decision": "killed",
                    "outcome": {"found": record is not None},
                }
            )
        if record is None:
            return False

        connection = record.connection
        for pending in list(self._pending.values()):
            if pending.connection is connection:
                pending.queue.put_nowait(_DISCONNECTED)

        try:
            await connection.send_json({"type": "session_terminated"})
        except Exception:
            pass  # best-effort : la connexion peut déjà être fermée

        close = getattr(connection, "close", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # best-effort : idem

        return True

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
        :class:`ClientDisconnectedError`, :class:`CommandTimeoutError` ou
        :class:`CommandDeniedError` (si une `CommandPolicy` refuse la
        commande — denylist/allowlist/quota).

        Si un `command_policy`/`audit_log` a été fourni au constructeur, la
        politique est vérifiée *avant* tout envoi au client, et chaque
        décision (refus immédiat ou issue finale de l'exécution) est
        journalisée dans l'audit.
        """
        record = await self._session_store.get(session_code)
        if record is None:
            raise SessionNotFoundError(f"session inconnue ou expirée : {session_code}")

        if self._command_policy is not None:
            decision = self._command_policy.check(session_code, tool, params)
            if not decision.allowed:
                self._record_audit(
                    session_code, tool, params, decision="denied", outcome={"reason": decision.reason}
                )
                raise CommandDeniedError(decision.reason or "commande refusée par la politique")

        connection = record.connection
        request_id = uuid.uuid4().hex
        pending = _PendingRequest(connection=connection)
        self._pending[request_id] = pending
        effective_timeout = timeout if timeout is not None else self._default_command_timeout
        outcome: dict[str, Any] = {}

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
                outcome = {"error": "client_disconnected"}
                raise ClientDisconnectedError(
                    f"impossible d'envoyer la commande au client : {exc}"
                ) from exc

            while True:
                try:
                    item = await asyncio.wait_for(pending.queue.get(), timeout=effective_timeout)
                except asyncio.TimeoutError as exc:
                    outcome = {"error": "timeout"}
                    raise CommandTimeoutError(
                        f"timeout en attente de la réponse à la commande {request_id}"
                    ) from exc

                if item is _DISCONNECTED:
                    outcome = {"error": "client_disconnected"}
                    raise ClientDisconnectedError(
                        "client déconnecté pendant l'exécution de la commande"
                    )

                chunk, final = item
                yield chunk
                if final:
                    if chunk.get("type") == "result":
                        outcome = {"exit_code": chunk.get("exit_code"), "error": chunk.get("error")}
                    elif chunk.get("type") == "approval_response":
                        outcome = {"approved": chunk.get("approved")}
                    return
        finally:
            self._pending.pop(request_id, None)
            self._record_audit(session_code, tool, params, decision="allowed", outcome=outcome)

    def _record_audit(
        self, session_code: str, tool: str, params: dict[str, Any], decision: str, outcome: dict[str, Any]
    ) -> None:
        if self._audit_log is None:
            return
        self._audit_log.record(
            {
                "session_code": session_code,
                "tool": tool,
                "params": params,
                "decision": decision,
                "outcome": outcome,
            }
        )
