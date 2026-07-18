"""Harnais partagé pour les tests e2e bout-en-bout (`tests/e2e/`).

Contrairement à `tests/relay/test_integration.py` (qui pilote le broker
directement, ou un "faux client" en `websockets` brut) et à
`tests/relay/test_mcp_oauth.py` (qui appelle les outils MCP en mémoire ou via
un transport ASGI in-process), les tests de ce dossier relient les **trois
vrais composants** :

- le relay réel (`relay.app.create_app` + `uvicorn.Server` sur un port TCP
  éphémère réel — même technique que `test_integration.py`, mais ici le
  canal `/ws/client` est piloté par un vrai processus, pas un client `websockets`
  de test) ;
- le **vrai binaire client Go compilé** (`client/`), lancé en sous-processus
  réel, qui se connecte en sortant au relay et exécute réellement les
  commandes reçues ;
- un **client MCP réel** (SDK officiel `mcp.client.streamable_http`), qui
  joue le rôle du harnais et appelle les outils exposés sur `/mcp`.

Voir `tests/e2e/README.md` pour comment lancer ces tests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from relay.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_DIR = REPO_ROOT / "client"

# Jetons/tokens de test, sans rapport avec un quelconque déploiement réel.
CLIENT_TOKEN = "e2e-client-token"
STATIC_MCP_TOKEN = "e2e-mcp-static-token"

# Timeouts par défaut des boucles d'attente active (pas de sleep fixe aveugle).
DEFAULT_STARTUP_TIMEOUT = 10.0
DEFAULT_SESSION_CODE_TIMEOUT = 20.0
DEFAULT_SHUTDOWN_TIMEOUT = 5.0


def build_client_binary(tmp_path: Path) -> Path:
    """Compile le vrai binaire client Go (`go build`) dans `tmp_path`.

    Réutilisé par le fixture `client_binary` (session-scope) de `conftest.py`.
    Lève `pytest.fail(...)` avec la sortie complète de `go build` en cas
    d'échec, plutôt qu'une `CalledProcessError` peu lisible.
    """
    binary = tmp_path / "claude-distant-client"
    proc = subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=CLIENT_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(
            "echec de compilation du client Go (go build -o ... . dans client/):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    if not binary.exists():
        pytest.fail("go build a réussi mais le binaire attendu est introuvable: " + str(binary))
    return binary


class RunningRelay:
    """Lance une vraie app relay (`uvicorn.Server` réel, port TCP éphémère)
    dans la boucle asyncio du test — même technique que
    `tests/relay/test_integration.py`, factorisée ici en context manager
    asynchrone réutilisable par les deux modes d'auth MCP.

    Usage :
        async with RunningRelay(client_token=..., mcp_bearer_token=..., ...) as relay:
            ...  # relay.ws_url, relay.mcp_url, relay.app
    """

    def __init__(self, **create_app_kwargs: Any) -> None:
        self._kwargs = create_app_kwargs
        self.app = None
        self.port: int | None = None
        self._server: Any = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "RunningRelay":
        import uvicorn  # import différé : évite le coût à la collecte des tests

        self.app = create_app(**self._kwargs)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        deadline = time.monotonic() + DEFAULT_STARTUP_TIMEOUT
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("le relay uvicorn ne s'est jamais annoncé démarré (server.started)")
            await asyncio.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        await self._wait_healthy()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        assert self._server is not None and self._task is not None
        self._server.should_exit = True
        await self._task

    async def _wait_healthy(self, timeout: float = DEFAULT_STARTUP_TIMEOUT) -> None:
        """Poll actif de `/healthz` : le relay accepte des connexions TCP dès
        `server.started`, mais on vérifie ici que la pile ASGI répond
        vraiment, sans sleep fixe."""
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(f"http://127.0.0.1:{self.port}/healthz", timeout=1.0)
                    if r.status_code == 200:
                        return
                except Exception as exc:  # noqa: BLE001 - poll robuste, on retente jusqu'au timeout
                    last_exc = exc
                await asyncio.sleep(0.05)
        raise RuntimeError(f"le relay n'a jamais répondu 200 sur /healthz (dernière erreur: {last_exc})")

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/client"

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


class RunningClient:
    """Sous-processus réel du binaire client Go compilé, pointé sur un relay
    de test (`--url`/`--token`/`--policy`, cf. `client/main.go`).

    Usage :
        async with RunningClient(binary, relay.ws_url, CLIENT_TOKEN) as client:
            code = await client.read_session_code()
    """

    def __init__(self, binary: Path, url: str, token: str, policy: str = "auto") -> None:
        self._binary = binary
        self._url = url
        self._token = token
        self._policy = policy
        self.proc: asyncio.subprocess.Process | None = None

    async def __aenter__(self) -> "RunningClient":
        self.proc = await asyncio.create_subprocess_exec(
            str(self._binary),
            "--url", self._url,
            "--token", self._token,
            "--policy", self._policy,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=DEFAULT_SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()

    async def read_session_code(self, timeout: float = DEFAULT_SESSION_CODE_TIMEOUT) -> str:
        """Lit la sortie réelle du process client jusqu'à voir le code de
        session à 9 chiffres qu'il affiche (`printSessionCode` dans
        `client/main.go`, ex. "Code de session : 784 123 678"), le parse et
        retire les espaces. Boucle d'attente active bornée par `timeout` —
        jamais de sleep fixe aveugle."""
        assert self.proc is not None and self.proc.stdout is not None
        pattern = re.compile(r"Code de session\s*:\s*([0-9\s]{9,})")
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "code de session jamais vu dans la sortie du client client dans le délai imparti "
                    f"({timeout}s), sortie capturée:\n" + "".join(seen)
                )
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if not line:
                raise RuntimeError(
                    "le process client s'est terminé avant d'afficher un code de session, sortie capturée:\n"
                    + "".join(seen)
                )
            text = line.decode(errors="replace")
            seen.append(text)
            m = pattern.search(text)
            if m:
                digits = re.sub(r"\D", "", m.group(1))
                if len(digits) == 9:
                    return digits


@contextlib.asynccontextmanager
async def mcp_client_session(url: str, token: str | None) -> AsyncIterator[ClientSession]:
    """Ouvre une vraie session MCP Streamable HTTP (SDK officiel `mcp`)
    contre `url`, avec `Authorization: Bearer <token>` — joue le rôle du
    harnais, exactement comme un vrai client MCP (Claude) le ferait."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url, headers=headers) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Appelle un outil MCP réel via la session et retourne son résultat
    structuré (dict), qu'il soit porté par `structuredContent` ou encodé en
    JSON dans le premier bloc de contenu texte."""
    result = await session.call_tool(name, arguments)
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    return json.loads(result.content[0].text)  # type: ignore[union-attr]
