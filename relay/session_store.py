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
from typing import Any, Callable, Sequence

CODE_LENGTH = 9
CODE_UPPER_BOUND = 10**CODE_LENGTH
MAX_CREATE_ATTEMPTS = 100


def generate_session_code() -> str:
    """Génère un code de session à 9 chiffres (zéro-paddé), cryptographiquement aléatoire."""
    return f"{secrets.randbelow(CODE_UPPER_BOUND):0{CODE_LENGTH}d}"


def _is_valid_code_syntax(code: str) -> bool:
    """Un code de session valide est une chaîne d'exactement 9 chiffres décimaux."""
    return len(code) == CODE_LENGTH and code.isdigit()


def normalize_capabilities(capabilities: Sequence[str] | None) -> tuple[str, ...]:
    """Normalise une liste de capacités déclarées en tuple immuable.

    `None` (ancien client, qui n'envoie pas le champ) devient `()` : l'absence
    de capacité est la valeur sûre, celle qui interdit tout dispatch d'outil
    « nouvelle génération » vers ce client (cf. `mcp_server._require_capability`).
    """
    if not capabilities:
        return ()
    return tuple(capabilities)


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
    # Capacités optionnelles déclarées par le client à `register` (ex.
    # `("file_transfer",)`). Ajouté **en dernier**, avec un défaut, pour ne
    # casser aucune construction positionnelle existante : un client déjà
    # déployé n'annonce rien et reste donc à `()`.
    capabilities: tuple[str, ...] = ()


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
        *,
        capabilities: Sequence[str] | None = None,
        desired_code: str | None = None,
    ) -> str:
        """Enregistre une nouvelle connexion et retourne le code attribué (unique).

        `capabilities` est keyword-only et optionnel : les appelants historiques
        (à cinq arguments) restent valides et obtiennent `()`.

        `desired_code` (keyword-only, optionnel — adresse stable, cf.
        `docs/PROTOCOL.md`) est honoré tel quel s'il est syntaxiquement valide
        (exactement 9 chiffres) **et libre** ; sinon le tirage aléatoire habituel
        s'applique. Premier arrivé, premier servi : aucune implémentation ne
        doit évincer une connexion existante détentrice du code, ce qui
        ouvrirait un détournement de session à un client se contentant de
        rejouer le code d'un tiers.
        """

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
    """Implémentation en mémoire process : dict + `asyncio.Lock`.

    Course à la reconnexion (cf. `docs/PROTOCOL.md`) : la session reste
    éphémère (TTL, heartbeat, suppression à la déconnexion — rien de tout ça
    ne change ici), seule l'adresse est stable. Si un client se reconnecte
    avant que `remove_by_connection` n'ait nettoyé son ancien enregistrement
    (WebSocket morte pas encore constatée côté serveur), le code souhaité
    apparaît occupé et cette nouvelle session obtient un code aléatoire.
    Fenêtre étroite mais réelle, et c'est un repli sûr : la seule alternative
    serait d'évincer l'ancienne connexion sur simple déclaration du nouveau
    client, ce qui reviendrait à laisser n'importe qui réclamer le code de
    quelqu'un d'autre.
    """

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
        *,
        capabilities: Sequence[str] | None = None,
        desired_code: str | None = None,
    ) -> str:
        async with self._lock:
            code = None
            # Adresse stable : le code souhaité n'est retenu que s'il est
            # syntaxiquement valide et libre à cet instant précis. Pas de
            # file d'attente, pas d'éviction — un client qui perd la course
            # (cf. docstring de la classe) retombe simplement sur le tirage
            # aléatoire, comme s'il n'avait rien demandé.
            if (
                isinstance(desired_code, str)
                and _is_valid_code_syntax(desired_code)
                and desired_code not in self._records
            ):
                code = desired_code
            else:
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
                capabilities=normalize_capabilities(capabilities),
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
