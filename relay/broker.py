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
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, Sequence

from .session_store import SessionStore

if TYPE_CHECKING:
    from .audit import AuditLog
    from .command_policy import CommandPolicy

DEFAULT_TTL_SECONDS = 1800
DEFAULT_COMMAND_TIMEOUT = 60

# Paramètres dont la valeur est volumineuse et sans intérêt pour l'audit : le
# contenu d'un `write_file` peut peser plusieurs mégaoctets de base64, qui
# gonfleraient inutilement le journal JSONL chaîné (et y recopieraient des
# données potentiellement sensibles). Seule leur taille est conservée.
_REDACTED_PARAM_KEYS = ("content_base64",)


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remplace les champs volumineux/binaires par un marqueur de taille.

    Travaille sur une copie superficielle : le dict d'origine (celui envoyé au
    client dans la trame `command`) n'est jamais muté.
    """
    if not any(key in params for key in _REDACTED_PARAM_KEYS):
        return params
    redacted = dict(params)
    for key in _REDACTED_PARAM_KEYS:
        value = redacted.get(key)
        if isinstance(value, str):
            redacted[key] = f"<base64 redacted: {len(value)} chars>"
    return redacted


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
        self,
        connection: ConnectionLike,
        os: str,
        hostname: str,
        version: str,
        capabilities: Sequence[str] | None = None,
    ) -> str:
        """Enregistre une connexion client fraîchement `register`-ée et retourne son code.

        `capabilities` est optionnel et en dernière position : un client déjà
        déployé n'en déclare aucune et se voit attribuer `()`, ce qui suffit au
        relay pour ne jamais lui envoyer d'outil qu'il ne connaît pas.
        """
        return await self._session_store.create(
            connection=connection,
            os=os,
            hostname=hostname,
            version=version,
            ttl_seconds=self._default_ttl_seconds,
            capabilities=capabilities,
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

        Gère `stream`, `file_chunk`, `result` et `approval_response` (tous
        portent `request_id`). Les autres types (`register`, `heartbeat`) sont
        gérés en amont par la boucle WS de `app.py`.

        **Seul `result` est final.** `approval_response` a longtemps été poussé
        comme terminateur, ce qui était un bug : sous `policy=confirm`, le
        client émet son approbation *avant* d'exécuter la commande approuvée,
        si bien que le harnais recevait un résultat vide et que les
        `stream`/`result` suivants étaient jetés (requête déjà retirée de
        `_pending`). Le client envoie systématiquement un `result` dans les
        quatre branches (auto, deny, confirm refusé, confirm approuvé) : le
        traiter comme unique terminateur est donc aussi rétrocompatible avec
        les clients déjà déployés.
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
        elif msg_type == "file_chunk":
            # Tranche de fichier (lecture chunkée client→relay), routée comme
            # `stream` : non finale, la séquence est close par le `result`.
            chunk = {"type": "file_chunk", "seq": message.get("seq"), "data": message.get("data")}
            await pending.queue.put((chunk, False))
        elif msg_type == "result":
            chunk = {
                "type": "result",
                "exit_code": message.get("exit_code"),
                "error": message.get("error"),
                # Champ optionnel (transfert de fichiers) : `None` pour un
                # ancien client, que les agrégateurs de commande ignorent.
                "meta": message.get("meta"),
            }
            await pending.queue.put((chunk, True))
        elif msg_type == "approval_response":
            chunk = {"type": "approval_response", "approved": message.get("approved")}
            await pending.queue.put((chunk, False))

    # -- API interne pour la couche MCP ------------------------------------

    async def dispatch_command(
        self,
        session_code: str,
        tool: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Envoie une commande au client ciblé et streame les chunks agrégés.

        Yield des dicts `{"type": "stream", ...}` / `{"type": "file_chunk", ...}`
        / `{"type": "approval_response", ...}` puis un dernier
        `{"type": "result", ...}`, seul terminateur du flux (y compris quand la
        commande a été refusée localement : le client émet alors un `result`
        portant `refused_by_user`/`refused_by_policy`).
        Lève :class:`SessionNotFoundError`,
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
                    # Seul `result` est final (cf. handle_client_message) :
                    # l'issue auditée provient donc toujours du client lui-même,
                    # y compris après une approbation ou un refus.
                    outcome = {"exit_code": chunk.get("exit_code"), "error": chunk.get("error")}
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
                "params": _redact_params(params),
                "decision": decision,
                "outcome": outcome,
            }
        )
