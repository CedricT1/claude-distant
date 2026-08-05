"""Tests bout-en-bout du transfert de fichiers (`read_file`/`write_file`) et de
la négociation de capacités.

Ces deux scénarios sont ceux que ni la suite `tests/relay/` (qui double le
client) ni la suite `client/*_test.go` (qui double le relay) ne peuvent
couvrir : ils confrontent les **noms de champs JSON réellement émis** par le
binaire Go à ceux réellement attendus par le relay Python. Un `seq` renommé, un
`meta.bytes_written` devenu `meta.written`, un `content_base64` mal orthographié
passeraient inaperçus des deux côtés pris isolément, et casseraient le transfert
en production.

1. `test_file_transfer_roundtrip` : aller-retour complet avec le **vrai**
   binaire client Go — un contenu binaire de ~500 Kio (donc découpé en
   plusieurs `file_chunk` à la relecture) est écrit puis relu, et son identité
   est vérifiée octet à octet **et** par SHA-256.
2. `test_legacy_client_without_capability` : un faux client « ancien » (celui
   d'avant la négociation de capacités, qui n'envoie pas `capabilities`) reçoit
   `unsupported_by_client` sans qu'aucune trame `command` inconnue ne lui soit
   jamais envoyée, et continue de servir `run_shell` exactement comme avant.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import random
from pathlib import Path
from typing import Any

import pytest
import websockets

from harness import (
    CLIENT_TOKEN,
    STATIC_MCP_TOKEN,
    RunningClient,
    RunningRelay,
    call_tool,
    mcp_client_session,
)

pytestmark = pytest.mark.e2e

# Contenu binaire déterministe (graine figée) et volontairement plus gros que
# `fileChunkBytes` (192 Kio, cf. client/filetransfer.go) : la relecture doit
# emprunter le chemin multi-`file_chunk`, réassemblé par `seq` côté relay.
PAYLOAD = random.Random(20260805).randbytes(500_000)
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


class LegacyClient:
    """Faux client « ancien » : le protocole d'avant la négociation de capacités.

    Volontairement en `websockets` brut plutôt qu'avec le binaire Go, qui
    déclare désormais `file_transfer` et ne peut donc plus jouer ce rôle. Il
    envoie un `register` **sans** champ `capabilities`, sert `run_shell` comme
    il l'a toujours fait (des `stream` puis un `result` sans `meta`) et mémorise
    les `tool` reçus, ce qui permet d'affirmer qu'aucune commande inconnue ne
    lui est jamais parvenue.
    """

    def __init__(self, ws_url: str, token: str, version: str = "0.1.0") -> None:
        self._ws_url = ws_url
        self._token = token
        self._version = version
        self.session_code = ""
        self.received_tools: list[str] = []
        self._ws: Any = None
        self._connect_cm: Any = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "LegacyClient":
        self._connect_cm = websockets.connect(
            self._ws_url,
            additional_headers={"Authorization": f"Bearer {self._token}"},
            proxy=None,
        )
        self._ws = await self._connect_cm.__aenter__()
        await self._ws.send(
            json.dumps(
                {
                    "type": "register",
                    "os": "linux",
                    "hostname": "legacy-host",
                    "version": self._version,
                }
            )
        )
        reply = json.loads(await self._ws.recv())
        assert reply["type"] == "registered"
        self.session_code = reply["session_code"]
        self._task = asyncio.create_task(self._serve())
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._connect_cm.__aexit__(*exc_info)

    async def _serve(self) -> None:
        """Boucle de service : répond aux `command` comme un client 0.1.0."""
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("type") != "command":
                continue
            tool = message.get("tool", "")
            self.received_tools.append(tool)
            request_id = message.get("request_id")
            if tool == "run_shell":
                await self._ws.send(
                    json.dumps(
                        {
                            "type": "stream",
                            "request_id": request_id,
                            "stream": "stdout",
                            "data": "legacy-ok\n",
                        }
                    )
                )
                # Un `result` d'ancien client ne porte évidemment pas de `meta`.
                await self._ws.send(
                    json.dumps(
                        {
                            "type": "result",
                            "request_id": request_id,
                            "exit_code": 0,
                            "error": None,
                        }
                    )
                )
            else:
                await self._ws.send(
                    json.dumps(
                        {
                            "type": "result",
                            "request_id": request_id,
                            "exit_code": 1,
                            "error": f"outil inconnu: {tool}",
                        }
                    )
                )


async def test_file_transfer_roundtrip(client_binary, tmp_path: Path):
    """`write_file` puis `read_file` : le contenu et le SHA-256 doivent survivre
    intacts à l'aller-retour harnais → relay → client Go → disque → retour."""
    target = tmp_path / "sous" / "dossier" / "charge.bin"

    async with RunningRelay(
        client_token=CLIENT_TOKEN,
        mcp_bearer_token=STATIC_MCP_TOKEN,
        session_ttl_seconds=60,
    ) as relay:
        async with RunningClient(client_binary, relay.ws_url, CLIENT_TOKEN) as client:
            session_code = await client.read_session_code()

            async with mcp_client_session(relay.mcp_url, STATIC_MCP_TOKEN) as mcp:
                # 0. Le vrai binaire annonce bien la capacité à son `register` :
                #    c'est elle qui débloque les deux outils ci-dessous.
                connect = await call_tool(mcp, "connect_session", {"session_code": session_code})
                assert connect["status"] == "connected"
                assert "file_transfer" in connect["capabilities"]

                # 1. Écriture : une seule trame `command` porte tout le base64,
                #    `create_dirs` fabrique l'arborescence, `mode` s'applique.
                written = await call_tool(
                    mcp,
                    "write_file",
                    {
                        "session_code": session_code,
                        "path": str(target),
                        "content_base64": base64.b64encode(PAYLOAD).decode("ascii"),
                        "mode": "0640",
                        "create_dirs": True,
                    },
                )
                assert written["status"] == "ok"
                assert written["path"] == str(target)
                assert written["bytes_written"] == len(PAYLOAD)
                assert written["sha256"] == PAYLOAD_SHA256

                # Le fichier existe vraiment sur ce disque, avec le bon contenu
                # et le bon mode : c'est le vrai executor Go qui l'a posé.
                assert target.read_bytes() == PAYLOAD
                assert target.stat().st_mode & 0o777 == 0o640

                # 2. Relecture complète : plusieurs `file_chunk` réassemblés.
                read_back = await call_tool(
                    mcp, "read_file", {"session_code": session_code, "path": str(target)}
                )
                assert read_back["status"] == "ok"
                assert read_back["encoding"] == "base64"
                assert read_back["size"] == len(PAYLOAD)
                assert read_back["truncated"] is False
                assert read_back["sha256"] == PAYLOAD_SHA256
                # L'identité du contenu, octet à octet, est le cœur du test :
                # un réassemblage désordonné ou un chunk perdu la casserait sans
                # forcément casser la taille.
                assert base64.b64decode(read_back["content_base64"]) == PAYLOAD

                # 3. Lecture partielle : `offset`/`max_bytes` ne sont transmis au
                #    client que lorsqu'ils sont signifiants, et le drapeau
                #    `truncated` remonte du `meta` produit par le client.
                slice_start, slice_len = 10, 100
                partial = await call_tool(
                    mcp,
                    "read_file",
                    {
                        "session_code": session_code,
                        "path": str(target),
                        "offset": slice_start,
                        "max_bytes": slice_len,
                    },
                )
                expected_slice = PAYLOAD[slice_start : slice_start + slice_len]
                assert partial["status"] == "ok"
                assert partial["size"] == slice_len
                assert partial["truncated"] is True
                assert partial["sha256"] == hashlib.sha256(expected_slice).hexdigest()
                assert base64.b64decode(partial["content_base64"]) == expected_slice

                # 4. Cas d'erreur remonté par le vrai client, avec son code stable.
                missing = await call_tool(
                    mcp,
                    "read_file",
                    {"session_code": session_code, "path": str(tmp_path / "jamais-cree")},
                )
                assert missing["status"] == "error"
                assert missing["error"] == "file_not_found"


async def test_legacy_client_without_capability(client_binary):
    """Non-régression : un client déjà déployé (aucune capacité déclarée) reste
    pleinement utilisable et se voit refuser les outils fichiers proprement.

    `client_binary` n'est pas utilisé ici — le client est volontairement l'ancien
    protocole — mais le fixture reste demandé pour que ce module partage le même
    plan de test que les autres fichiers e2e.
    """
    async with RunningRelay(
        client_token=CLIENT_TOKEN,
        mcp_bearer_token=STATIC_MCP_TOKEN,
        session_ttl_seconds=60,
    ) as relay:
        async with LegacyClient(relay.ws_url, CLIENT_TOKEN) as legacy:
            session_code = legacy.session_code

            async with mcp_client_session(relay.mcp_url, STATIC_MCP_TOKEN) as mcp:
                # 1. La session est parfaitement normale, simplement sans capacité.
                connect = await call_tool(mcp, "connect_session", {"session_code": session_code})
                assert connect["status"] == "connected"
                assert connect["capabilities"] == []

                # 2. Les deux outils fichiers sont refusés au niveau du relay,
                #    avec un diagnostic actionnable pour le harnais.
                for tool, arguments in (
                    ("read_file", {"path": "/etc/hostname"}),
                    ("write_file", {"path": "/tmp/jamais-ecrit", "content_base64": "AAA="}),
                ):
                    refused = await call_tool(mcp, tool, {"session_code": session_code, **arguments})
                    assert refused["status"] == "error"
                    assert refused["error"] == "unsupported_by_client"
                    assert refused["client_version"] == "0.1.0"
                    assert refused["capabilities"] == []
                    assert "0.2.0" in refused["detail"]

                # 3. Le refus est bien décidé *avant* tout dispatch : l'ancien
                #    client n'a jamais reçu de trame `command` qu'il ne saurait
                #    pas traiter. C'est toute la raison d'être de la garde.
                assert legacy.received_tools == []

                # 4. Et le flot historique continue de fonctionner à l'identique.
                shell_result = await call_tool(
                    mcp,
                    "run_shell",
                    {"session_code": session_code, "command": "echo legacy-ok", "shell": "auto"},
                )
                assert shell_result["status"] == "ok"
                assert shell_result["exit_code"] == 0
                assert shell_result["error"] is None
                assert shell_result["stdout"] == "legacy-ok\n"
                # Le `meta` optionnel ne doit pas s'inviter dans la sortie des
                # outils de commande quand le client n'en envoie pas.
                assert "meta" not in shell_result
                assert legacy.received_tools == ["run_shell"]
