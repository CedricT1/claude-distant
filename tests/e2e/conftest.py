"""Fixtures partagées des tests e2e bout-en-bout (`tests/e2e/`).

Voir `tests/e2e/harness.py` pour l'implémentation des context managers réels
(relay, client Go, client MCP) et `tests/e2e/README.md` pour comment lancer
ces tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness import CLIENT_DIR, build_client_binary


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: test bout-en-bout réel (relay + vrai binaire client Go + vrai client MCP), plus lent",
    )


@pytest.fixture(scope="session")
def client_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Le vrai binaire client Go compilé, une seule fois pour toute la session pytest.

    Réutilise `client/dist/claude-distant-client-linux-amd64` s'il existe déjà
    (produit par `make dist`, cf. `client/Makefile`) et que la cible du build
    correspond bien à l'hôte d'exécution (linux/amd64) ; sinon compile via
    `go build` dans un répertoire temporaire dédié à la session de test.
    """
    prebuilt = CLIENT_DIR / "dist" / "claude-distant-client-linux-amd64"
    if prebuilt.exists():
        import platform

        if platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64"):
            return prebuilt

    out_dir = tmp_path_factory.mktemp("client-build")
    return build_client_binary(out_dir)
