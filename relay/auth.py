"""Validation des jetons Bearer (client WS et MCP).

Le MVP utilise des tokens pré-partagés (variables d'environnement). La
comparaison est faite en temps constant pour limiter les attaques par
timing. La migration OAuth 2.1 (phase 5 du plan) remplacera ce module par
une vérification d'introspection/JWT côté MCP, sans changer l'API des
fonctions ci-dessous côté appelant.
"""
from __future__ import annotations

import hmac

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
