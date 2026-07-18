"""Validation des jetons Bearer (client WS et MCP).

Le MVP utilise des tokens pré-partagés (variables d'environnement). La
comparaison est faite en temps constant pour limiter les attaques par
timing. La migration OAuth 2.1 (phase 5 du plan) remplacera ce module par
une vérification d'introspection/JWT côté MCP, sans changer l'API des
fonctions ci-dessous côté appelant.

## Mode d'authentification client : `shared` vs `per_session`

Le canal client↔relay (`/ws/client`) supporte deux modes, sélectionnés via
`CLIENT_AUTH_MODE` (cf. `relay/app.py`) :

- `shared` (défaut, compat MVP) : tous les clients s'authentifient avec le
  même jeton pré-partagé `CLIENT_TOKEN`. Comportement inchangé du handshake
  initial du protocole (`docs/PROTOCOL.md` §1).
- `per_session` : le relay émet un jeton **court, à usage unique, lié à une
  future session** via :class:`PerSessionTokenStore` (secret aléatoire,
  TTL = TTL de session par défaut). Le client se connecte avec ce jeton
  (`Authorization: Bearer <jeton>`) au lieu du `CLIENT_TOKEN` global ; le
  jeton est consommé (invalidé) dès le premier `register` réussi, si bien
  qu'une connexion compromise/rejouée ne peut pas être réutilisée pour une
  autre session.

  TODO (vague OAuth 2.1 qui suivra, cf. `docs/PLAN.md` Phase 5) : ce mode
  n'a pour l'instant aucun canal d'émission exposé au harnais (pas d'outil
  MCP `issue_client_token`) — l'émission se fait en appelant directement
  `PerSessionTokenStore.issue(...)` côté opérateur/déploiement. La vraie
  migration OAuth 2.1 remplacera ce mécanisme manuel par un flux
  d'émission/révocation standard (ex. token exchange, introspection) piloté
  depuis le harnais, sans changer la forme de `verify_client_token`.

:func:`verify_client_token` est le point d'entrée unique utilisé par
`relay/app.py` au handshake WS ; il bascule entre les deux comportements
selon `mode`, sans jamais changer la sémantique du mode `shared` existant.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Callable

_BEARER_PREFIX = "bearer "


class AuthError(Exception):
    """Levée quand un jeton Bearer est absent ou invalide."""


def extract_bearer_token(header_value: str | None) -> str | None:
    """Extrait le jeton d'un en-tête ``Authorization: Bearer <token>``.

    Retourne ``None`` si l'en-tête est absent, mal formé, n'utilise pas le
    schéma ``Bearer`` ou si le jeton est vide.
    """
    if not header_value:
        return None
    if not header_value.lower().startswith(_BEARER_PREFIX):
        return None
    token = header_value[len(_BEARER_PREFIX) :].strip()
    return token or None


def verify_token(provided: str | None, expected: str | None) -> bool:
    """Compare deux jetons en temps constant.

    Un ``expected`` vide/``None`` est toujours refusé, même si ``provided``
    est également vide : cela évite qu'une mauvaise configuration (token
    d'environnement non défini) n'ouvre l'accès à tout le monde.
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def require_bearer(header_value: str | None, expected: str | None) -> None:
    """Lève :class:`AuthError` si l'en-tête ne porte pas le bon jeton Bearer."""
    token = extract_bearer_token(header_value)
    if not verify_token(token, expected):
        raise AuthError("jeton Bearer manquant ou invalide")


class PerSessionTokenStore:
    """Jetons client à usage unique, à durée de vie limitée (mode `per_session`).

    Chaque jeton est un secret aléatoire (`secrets.token_urlsafe`), émis via
    :meth:`issue` avec un TTL (typiquement le TTL de session par défaut du
    relay), et validé via :meth:`validate`. Le jeton est destiné à être
    consommé (:meth:`consume`) dès qu'il a servi à authentifier une connexion
    WS qui a réussi son `register` — voir docstring de module pour le
    mécanisme complet.

    Thread-safe (`threading.Lock`), utilisable depuis une coroutine asyncio
    unique comme depuis plusieurs threads.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tokens: dict[str, float] = {}  # token -> expires_at
        self._lock = threading.Lock()

    def issue(self, ttl_seconds: float) -> str:
        """Génère et enregistre un nouveau jeton, valide `ttl_seconds` secondes."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = self._clock() + ttl_seconds
        return token

    def validate(self, token: str | None) -> bool:
        """`True` si `token` est un jeton connu et non expiré."""
        if not token:
            return False
        with self._lock:
            expires_at = self._tokens.get(token)
            if expires_at is None:
                return False
            if expires_at <= self._clock():
                del self._tokens[token]
                return False
            return True

    def consume(self, token: str) -> None:
        """Invalide définitivement un jeton (usage unique). No-op si inconnu."""
        with self._lock:
            self._tokens.pop(token, None)


def verify_client_token(
    provided: str | None,
    mode: str,
    shared_token: str | None,
    per_session_store: "PerSessionTokenStore | None",
) -> bool:
    """Valide le jeton Bearer d'une connexion client WS selon `CLIENT_AUTH_MODE`.

    - `mode == "shared"` (défaut) : identique à `verify_token(provided,
      shared_token)` — comportement du handshake MVP inchangé.
    - `mode == "per_session"` : délègue à `per_session_store.validate(provided)`
      ; refuse tout si `per_session_store` est `None` (mauvaise config, sûr
      par défaut plutôt qu'ouvert).
    """
    if mode == "per_session":
        if per_session_store is None:
            return False
        return per_session_store.validate(provided)
    return verify_token(provided, shared_token)
