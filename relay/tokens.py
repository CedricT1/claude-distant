"""Émetteur de jetons opérateur pour le mode `MCP_AUTH_MODE=oauth` (phase 5).

Utilisation en CLI, côté déploiement/opérateur (jamais exposé au harnais ou au
réseau) :

    python -m relay.tokens issue --sub harness-operateur \\
        --scopes session:connect,command:execute,session:terminate,client:provision \\
        --ttl 3600

Affiche le JWT signé sur stdout (rien d'autre : facile à capturer dans un
script). Le secret est lu depuis `--secret`, ou par défaut depuis la variable
d'environnement `MCP_JWT_SECRET` — jamais de valeur par défaut implicite (voir
`relay/jwt_auth.py:issue_token`, qui lève `ValueError` si aucun des deux n'est
fourni).

Les scopes disponibles correspondent aux outils MCP (voir
`relay/mcp_server.py:TOOL_SCOPES`) :
  - `session:connect`   → `connect_session`
  - `command:execute`   → `run_command`, `run_shell`
  - `session:terminate` → `terminate_session`
  - `client:provision`  → `issue_client_token`
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .jwt_auth import DEFAULT_ALGORITHM, DEFAULT_TTL_SECONDS, issue_token

__all__ = ["issue_token", "main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m relay.tokens",
        description="Émet un jeton Bearer JWT pour le harnais MCP (mode MCP_AUTH_MODE=oauth).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue_parser = subparsers.add_parser("issue", help="Émet un nouveau jeton")
    issue_parser.add_argument(
        "--sub", required=True, help="Identifiant du principal (ex. 'harness-operateur')"
    )
    issue_parser.add_argument(
        "--scopes",
        required=True,
        help=(
            "Scopes séparés par des virgules, ex. "
            "session:connect,command:execute,session:terminate,client:provision"
        ),
    )
    issue_parser.add_argument(
        "--ttl",
        type=float,
        default=DEFAULT_TTL_SECONDS,
        help=f"Durée de vie en secondes (défaut {DEFAULT_TTL_SECONDS:.0f})",
    )
    issue_parser.add_argument(
        "--secret",
        default=None,
        help="Secret HS256 (défaut : variable d'environnement MCP_JWT_SECRET)",
    )
    issue_parser.add_argument("--algorithm", default=DEFAULT_ALGORITHM)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Point d'entrée CLI. Retourne le code de sortie du processus."""
    args = _build_parser().parse_args(argv)

    if args.command == "issue":
        scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip()]
        token = issue_token(
            args.sub,
            scopes,
            ttl_seconds=args.ttl,
            secret=args.secret,
            algorithm=args.algorithm,
        )
        print(token)
        return 0

    return 1  # pragma: no cover - argparse `required=True` empêche ce cas


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
