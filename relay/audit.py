"""Journal d'audit immuable (append-only, JSONL, chaîné par hash).

Chaque appel à :meth:`AuditLog.record` ajoute une ligne JSON au fichier
configuré (`AUDIT_LOG_PATH`, défaut `./audit.log`). Chaque entrée porte le
hash SHA-256 de l'entrée précédente (`prev_hash`) et son propre hash
(`hash`), calculé sur son contenu canonique (JSON trié par clé). Toute
modification, suppression ou réordonnancement d'une entrée casse la chaîne
et est détectable via :func:`verify_chain` — c'est ce qui rend le journal
« tamper-evident » (falsification détectable) sans nécessiter de stockage
externe ou de signature asymétrique pour ce MVP.

Champs d'une entrée : `timestamp` (ISO8601 UTC), `session_code`, `tool`,
`params_summary` (résumé tronqué des paramètres — jamais les données brutes
massives), `decision` (`allowed`/`denied`/`killed`), `outcome` (exit_code /
erreur / raison), `prev_hash`, `hash`.

Thread-safety : un unique `threading.Lock` protège la lecture de
`_prev_hash` et l'écriture du fichier ; ce verrou convient aussi bien à un
usage depuis une seule coroutine asyncio (les appels sont synchrones, sans
point de suspension à l'intérieur de la section critique) qu'à un usage
multi-thread réel.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
DEFAULT_AUDIT_LOG_PATH = "./audit.log"
_MAX_PARAM_VALUE_LEN = 200  # troncature par champ pour éviter de recopier des données massives


def _default_path() -> Path:
    return Path(os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))


def _summarize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Résume les paramètres d'une commande : tronque toute valeur trop longue.

    Évite de recopier des données massives (ex. un script de plusieurs Mo
    passé à `run_shell`) dans le journal d'audit — seul un résumé identifiable
    est conservé.
    """
    summary: dict[str, Any] = {}
    for key, value in params.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > _MAX_PARAM_VALUE_LEN:
            summary[key] = f"{text[:_MAX_PARAM_VALUE_LEN]}... (truncated, {len(text)} chars)"
        else:
            summary[key] = value
    return summary


def _canonical_json(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)


def _compute_hash(entry_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(entry_without_hash).encode("utf-8")).hexdigest()


class AuditLog:
    """Journal d'audit append-only, chaîné par hash, sur un fichier JSONL."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._lock = threading.Lock()
        self._prev_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self._path.exists():
            return GENESIS_HASH
        last_hash = GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                last_hash = entry.get("hash", last_hash)
        return last_hash

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        """Ajoute une entrée d'audit chaînée et retourne l'entrée écrite (avec `hash`)."""
        entry = dict(event)
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        if "params" in entry:
            entry["params_summary"] = _summarize_params(entry.pop("params") or {})

        with self._lock:
            entry["prev_hash"] = self._prev_hash
            entry_hash = _compute_hash(entry)
            entry["hash"] = entry_hash

            if self._path.parent != Path(""):
                self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(_canonical_json(entry) + "\n")

            self._prev_hash = entry_hash

        return entry


def verify_chain(path: str | os.PathLike) -> bool:
    """Valide l'intégrité complète de la chaîne de hash d'un journal d'audit.

    Retourne `True` si chaque entrée référence correctement le hash de la
    précédente et que son propre hash recalculé correspond à celui stocké
    (donc si aucune ligne n'a été modifiée, supprimée ou réordonnée).
    Un fichier absent ou vide est considéré valide (rien à falsifier).
    """
    path = Path(path)
    if not path.exists():
        return True

    expected_prev_hash = GENESIS_HASH
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False

            stored_hash = entry.get("hash")
            if entry.get("prev_hash") != expected_prev_hash:
                return False

            entry_without_hash = {k: v for k, v in entry.items() if k != "hash"}
            if _compute_hash(entry_without_hash) != stored_hash:
                return False

            expected_prev_hash = stored_hash

    return True
