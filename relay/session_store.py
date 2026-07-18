"""Store de session en mémoire (MVP) avec TTL, verrou asyncio et abstraction.

Le MVP garde tout en mémoire process (dict + `asyncio.Lock`). L'interface
:class:`SessionStore` est volontairement minimale pour permettre un
remplacement futur par un store Redis (partagé multi-instance, cf.
`docs/PLAN.md` §2) sans changer les appelants (`broker.py`).
"""
from __future__ import annotations

import abc
import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

CODE_LENGTH = 9
CODE_UPPER_BOUND = 10**CODE_LENGTH
MAX_CREATE_ATTEMPTS = 100


def generate_session_code() -> str:
    """Génère un code de session à 9 chiffres (zéro-paddé), cryptographiquement aléatoire."""
    return f"{secrets.randbelow(CODE_UPPER_BOUND):0{CODE_LENGTH}d}"


@dataclass
class SessionRecord:
    """Enregistrement d'une session active : code → connexion client + métadonnées."""

    code: str
    connection: Any
    os: str
    hostname: str
    version: str
    created_at: float
    expires_at: float


class SessionStore(abc.ABC):
    """Interface abstraite d'un store de sessions (code 9 chiffres → connexion).

    Toutes les méthodes sont async pour permettre une implémentation réseau
    (Redis) transparente derrière la même API.
    """

    @abc.abstractmethod
    async def create(
        self,
        connection: Any,
        os: str,
        hostname: str,
        version: str,
        ttl_seconds: float,
    ) -> str:
        """Enregistre une nouvelle connexion et retourne le code attribué (unique)."""

    @abc.abstractmethod
    async def get(self, code: str) -> SessionRecord | None:
        """Retourne l'enregistrement associé au code, ou `None` si absent/expiré."""

    @abc.abstractmethod
    async def touch(self, code: str, ttl_seconds: float) -> bool:
        """Prolonge le TTL d'une session existante (heartbeat). `False` si code inconnu."""

    @abc.abstractmethod
    async def remove(self, code: str) -> None:
        """Supprime une session (no-op si le code est inconnu)."""

    @abc.abstractmethod
    async def remove_by_connection(self, connection: Any) -> None:
        """Supprime la/les session(s) associée(s) à une connexion (déconnexion client)."""

    @abc.abstractmethod
    async def terminate(self, code: str) -> SessionRecord | None:
        """Invalide explicitement une session (kill-switch).

        Contrairement à `remove` (nettoyage interne, silencieux), `terminate`
        représente une décision explicite (opérateur/harnais) de couper une
        session active. Retourne l'enregistrement supprimé (pour que
        l'appelant, ex. `Broker.terminate_session`, puisse notifier/fermer la
        connexion associée), ou `None` si le code était déjà inconnu/expiré.
        """


class InMemorySessionStore(SessionStore):
    """Implémentation en mémoire process : dict + `asyncio.Lock`."""

    def __init__(self, code_generator: Callable[[], str] = generate_session_code) -> None:
        self._records: dict[str, SessionRecord] = {}
        self._code_generator = code_generator
        self._lock = asyncio.Lock()

    async def create(
        self,
        connection: Any,
        os: str,
        hostname: str,
        version: str,
        ttl_seconds: float,
    ) -> str:
        async with self._lock:
            code = None
            for _ in range(MAX_CREATE_ATTEMPTS):
                candidate = self._code_generator()
                if candidate not in self._records:
                    code = candidate
                    break
            if code is None:
                raise RuntimeError("impossible de générer un code de session unique")
            now = time.monotonic()
            self._records[code] = SessionRecord(
                code=code,
                connection=connection,
                os=os,
                hostname=hostname,
                version=version,
                created_at=now,
                expires_at=now + ttl_seconds,
            )
            return code

    async def get(self, code: str) -> SessionRecord | None:
        async with self._lock:
            record = self._records.get(code)
            if record is None:
                return None
            if record.expires_at <= time.monotonic():
                del self._records[code]
                return None
            return record

    async def touch(self, code: str, ttl_seconds: float) -> bool:
        async with self._lock:
            record = self._records.get(code)
            if record is None:
                return False
            now = time.monotonic()
            if record.expires_at <= now:
                del self._records[code]
                return False
            record.expires_at = now + ttl_seconds
            return True

    async def remove(self, code: str) -> None:
        async with self._lock:
            self._records.pop(code, None)

    async def remove_by_connection(self, connection: Any) -> None:
        async with self._lock:
            for code, record in list(self._records.items()):
                if record.connection == connection:
                    del self._records[code]

    async def terminate(self, code: str) -> SessionRecord | None:
        async with self._lock:
            return self._records.pop(code, None)
