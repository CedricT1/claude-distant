"""Politique d'application côté serveur des commandes : allow/denylist + quotas.

`CommandPolicy.check(session_code, tool, params)` est appelé par
`Broker.dispatch_command` avant tout envoi de commande au client distant
(cf. `relay/broker.py`). Deux mécanismes indépendants :

1. **Allow/denylist** de commandes, appliquée sur le champ `command` des
   outils `run_command`/`run_shell` (les autres outils, ex. `system_info`,
   n'ont pas de `command` et ne sont donc jamais filtrés par motif). Les
   motifs sont des expressions régulières (`re.search`), ce qui couvre aussi
   bien un simple préfixe (`^apt`) qu'un motif plus riche. La **denylist est
   toujours prioritaire** : si une commande matche à la fois l'allowlist et
   la denylist, elle est refusée. Une allowlist non vide devient une liste
   *restrictive* : seules les commandes qui la matchent passent.

2. **Quotas par session** : nombre max de commandes sur la durée de vie de
   la session (`max_commands_per_session`) et limite de débit sur une
   fenêtre glissante de 60s (`rate_limit_per_minute`). Les quotas s'appliquent
   à *tous* les outils dispatchés (pas seulement `run_command`/`run_shell`),
   pour éviter qu'un usage abusif d'un outil sans motif filtrable ne
   contourne la protection. Une commande refusée (deny/allowlist) ne
   consomme pas le quota.

Configuration via variables d'environnement (`CommandPolicy.from_env()`) :
`COMMAND_DENYLIST` / `COMMAND_ALLOWLIST` (motifs séparés par `;`),
`MAX_COMMANDS_PER_SESSION`, `RATE_LIMIT_PER_MINUTE`. Non définies : politique
permissive (aucune restriction), pour rester compatible avec le MVP.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

RATE_LIMIT_WINDOW_SECONDS = 60.0
_COMMAND_TOOLS = ("run_command", "run_shell")


@dataclass
class Decision:
    """Résultat de `CommandPolicy.check` : autorisé ou non, avec une raison si refusé."""

    allowed: bool
    reason: str | None = None


def _parse_pattern_list(raw: str) -> list[str]:
    return [pattern for pattern in raw.split(";") if pattern]


def _parse_optional_int(raw: str | None) -> int | None:
    if not raw:
        return None
    return int(raw)


class CommandPolicy:
    """Allow/denylist + quotas par session, appliqués avant dispatch au client."""

    def __init__(
        self,
        denylist: list[str] | None = None,
        allowlist: list[str] | None = None,
        max_commands_per_session: int | None = None,
        rate_limit_per_minute: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._deny_patterns = [re.compile(p) for p in (denylist or [])]
        self._allow_patterns = [re.compile(p) for p in (allowlist or [])]
        self._max_commands_per_session = max_commands_per_session
        self._rate_limit_per_minute = rate_limit_per_minute
        self._clock = clock
        self._counts: dict[str, int] = {}
        self._timestamps: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "CommandPolicy":
        """Construit une politique à partir des variables d'environnement (voir docstring module)."""
        return cls(
            denylist=_parse_pattern_list(os.environ.get("COMMAND_DENYLIST", "")),
            allowlist=_parse_pattern_list(os.environ.get("COMMAND_ALLOWLIST", "")),
            max_commands_per_session=_parse_optional_int(os.environ.get("MAX_COMMANDS_PER_SESSION")),
            rate_limit_per_minute=_parse_optional_int(os.environ.get("RATE_LIMIT_PER_MINUTE")),
        )

    @staticmethod
    def _extract_command(tool: str, params: dict) -> str | None:
        if tool in _COMMAND_TOOLS:
            return params.get("command")
        return None

    def check(self, session_code: str, tool: str, params: dict) -> Decision:
        """Décide si la commande peut être dispatchée au client pour cette session."""
        command = self._extract_command(tool, params)

        with self._lock:
            if command is not None:
                for pattern in self._deny_patterns:
                    if pattern.search(command):
                        return Decision(
                            allowed=False,
                            reason=f"commande refusée par la denylist (motif : {pattern.pattern!r})",
                        )
                if self._allow_patterns and not any(
                    pattern.search(command) for pattern in self._allow_patterns
                ):
                    return Decision(
                        allowed=False,
                        reason="commande absente de l'allowlist (liste restrictive)",
                    )

            count = self._counts.get(session_code, 0)
            if (
                self._max_commands_per_session is not None
                and count >= self._max_commands_per_session
            ):
                return Decision(
                    allowed=False,
                    reason=(
                        f"quota de commandes dépassé pour la session "
                        f"({self._max_commands_per_session} max)"
                    ),
                )

            now = self._clock()
            timestamps = self._timestamps.setdefault(session_code, deque())
            while timestamps and now - timestamps[0] >= RATE_LIMIT_WINDOW_SECONDS:
                timestamps.popleft()
            if (
                self._rate_limit_per_minute is not None
                and len(timestamps) >= self._rate_limit_per_minute
            ):
                return Decision(
                    allowed=False,
                    reason=(
                        f"limite de débit dépassée "
                        f"({self._rate_limit_per_minute} commandes/minute max)"
                    ),
                )

            self._counts[session_code] = count + 1
            timestamps.append(now)
            return Decision(allowed=True)
