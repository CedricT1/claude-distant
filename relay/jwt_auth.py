"""Vérification et émission de jetons Bearer JWT pour le mode `MCP_AUTH_MODE=oauth`
(phase 5 — durcissement sécurité, cf. `docs/PLAN.md`).

## Pourquoi HS256 auto-contenu plutôt qu'un vrai serveur d'autorisation externe

`claude-distant` agit comme **Resource Server OAuth 2.1** (RFC 8707 / RFC 9728)
mais ne fait pas tourner de serveur d'autorisation (`/authorize`, `/token`, etc.) :
les jetons sont des JWT **auto-émis** par l'opérateur du relay via
:func:`issue_token` (ou `python -m relay.tokens issue`, voir `relay/tokens.py`),
signés avec un secret partagé HS256 (`MCP_JWT_SECRET`) que seul le relay connaît.
C'est un compromis pragmatique et **auto-testable sans infrastructure externe** :
le relay est à la fois l'émetteur et le vérifieur (« issuer == resource server »),
ce qui est raisonnable pour un déploiement à opérateur unique (le harnais et le
relay appartiennent au même opérateur). Une vraie fédération multi-émetteurs
(introspection RFC 7662, JWKS RS256/ES256, serveur d'autorisation tiers)
resterait la suite logique si `claude-distant` devait un jour servir plusieurs
opérateurs indépendants — non nécessaire pour ce MVP durci.

## Vérification

:class:`JWTTokenVerifier` implémente le protocole `mcp.server.auth.provider.
TokenVerifier` attendu nativement par le SDK MCP (`async def verify_token(self,
token: str) -> AccessToken | None`) : c'est le point d'intégration réel utilisé
par `relay.mcp_server.build_mcp_asgi_app(..., mode="oauth")` pour brancher
`mcp.server.auth` (`AuthSettings` + `token_verifier`) plutôt qu'un middleware
maison — voir la docstring de `relay/mcp_server.py` pour le détail du câblage
(`BearerAuthBackend`, `AuthContextMiddleware`, `RequireAuthMiddleware`).

Seul l'algorithme HS256 est accepté par défaut : un jeton signé avec un autre
algorithme (y compris `"none"`) est toujours rejeté, quel que soit son contenu.

Le JWT doit porter :
  - `sub` (str) : identifiant du principal (obligatoire, sinon rejeté)
  - `exp` (int, epoch) : expiration (obligatoire, vérifiée par PyJWT)
  - `scopes` (list[str], optionnel, défaut `[]`) : ex. `session:connect`,
    `command:execute`, `session:terminate`, `client:provision` (cf.
    `relay/mcp_server.py:TOOL_SCOPES`).
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

import jwt as pyjwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

DEFAULT_ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 3600.0


def issue_token(
    sub: str,
    scopes: Iterable[str],
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    secret: str | None = None,
    algorithm: str = DEFAULT_ALGORITHM,
    now: float | None = None,
) -> str:
    """Signe et retourne un JWT Bearer pour le harnais (mode oauth).

    `secret` par défaut lu depuis la variable d'environnement `MCP_JWT_SECRET` ;
    lève :class:`ValueError` si absent (jamais de secret implicite/vide, même
    comportement « sûr par défaut » que le reste du module `relay/auth.py`).
    """
    resolved_secret = secret if secret is not None else os.environ.get("MCP_JWT_SECRET")
    if not resolved_secret:
        raise ValueError(
            "MCP_JWT_SECRET manquant : impossible de signer un jeton sans secret"
        )
    issued_at = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
        "scopes": list(scopes),
    }
    return pyjwt.encode(payload, resolved_secret, algorithm=algorithm)


class JWTTokenVerifier(TokenVerifier):
    """Vérifie des jetons Bearer JWT HS256 signés par :func:`issue_token`.

    Implémente le protocole `mcp.server.auth.provider.TokenVerifier` du SDK
    MCP officiel : voir docstring de module pour le détail du câblage réel
    (`FastMCP(token_verifier=..., auth=AuthSettings(...))`).
    """

    def __init__(
        self,
        secret: str,
        algorithm: str = DEFAULT_ALGORITHM,
        issuer: str | None = None,
        audience: str | None = None,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience

    async def verify_token(self, token: str) -> AccessToken | None:
        """Décode et valide `token` ; retourne `None` si invalide, expiré, ou
        s'il manque le claim `sub` — jamais d'exception qui fuite vers le SDK."""
        try:
            payload = pyjwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "sub"]},
            )
        except pyjwt.PyJWTError:
            return None

        sub = payload.get("sub")
        if not sub:
            return None

        scopes = payload.get("scopes") or []
        if isinstance(scopes, str):
            scopes = scopes.split()

        exp = payload.get("exp")
        return AccessToken(
            token=token,
            client_id=str(sub),
            scopes=list(scopes),
            expires_at=int(exp) if exp is not None else None,
            subject=str(sub),
        )
