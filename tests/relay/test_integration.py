"""Test d'intégration bout-en-bout : vraie app FastAPI, vrai socket WebSocket.

Un serveur uvicorn est lancé en tâche de fond sur un port éphémère, dans la
même boucle asyncio que le test. Un « faux client » se connecte en WS réel
(`websockets`), effectue le handshake protocolaire (`register` →
`registered` avec code 9 chiffres), puis on pilote `broker.dispatch_command`
directement (comme le ferait la couche MCP) pour vérifier que la commande
envoyée est bien reçue côté "client" et que les `stream`/`result` renvoyés
sont correctement agrégés et corrélés par `request_id`.
"""
import asyncio
import json
import re

import pytest
import uvicorn
import websockets
import websockets.exceptions

from relay.app import create_app
from relay.broker import ClientDisconnectedError, SessionNotFoundError

CLIENT_TOKEN = "test-client-token"
MCP_TOKEN = "test-mcp-token"


@pytest.fixture
async def running_app():
    app = create_app(client_token=CLIENT_TOKEN, mcp_bearer_token=MCP_TOKEN, session_ttl_seconds=30)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield app, port
    finally:
        server.should_exit = True
        await task


def _uri(port: int) -> str:
    return f"ws://127.0.0.1:{port}/ws/client"


def _connect(port: int, token: str | None = CLIENT_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return websockets.connect(_uri(port), additional_headers=headers, proxy=None)


async def _call_tool(app, name: str, arguments: dict):
    """Appelle un outil MCP de l'app réelle et retourne son résultat structuré."""
    result = await app.state.mcp.call_tool(name, arguments)
    if isinstance(result, tuple):
        _content, structured = result
        return structured
    return json.loads(result[0].text)


async def _register(ws, **extra) -> str:
    """Effectue le handshake `register` → `registered` et retourne le code de session."""
    message = {"type": "register", "os": "linux", "hostname": "h", "version": "0.1.0"}
    message.update(extra)
    await ws.send(json.dumps(message))
    reply = json.loads(await ws.recv())
    assert reply["type"] == "registered"
    return reply["session_code"]


class TestPerSessionClientAuth:
    """Mode `CLIENT_AUTH_MODE=per_session` : jeton client court, lié à la session."""

    async def test_shared_mode_is_default_and_unaffected(self, running_app):
        # Le fixture `running_app` utilise déjà create_app sans client_auth_mode :
        # comportement `shared` inchangé (voir TestWebSocketRegistration ci-dessous).
        _app, port = running_app
        async with _connect(port) as ws:
            await ws.send(json.dumps({"type": "register", "os": "linux", "hostname": "h", "version": "1"}))
            reply = json.loads(await ws.recv())
            assert reply["type"] == "registered"

    async def test_per_session_mode_accepts_issued_token(self):
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            session_ttl_seconds=30,
            client_auth_mode="per_session",
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            issued_token = app.state.client_token_store.issue(ttl_seconds=30)
            async with _connect(port, token=issued_token) as ws:
                await ws.send(
                    json.dumps({"type": "register", "os": "linux", "hostname": "h", "version": "1"})
                )
                reply = json.loads(await ws.recv())
                assert reply["type"] == "registered"
        finally:
            server.should_exit = True
            await task

    async def test_per_session_mode_rejects_shared_client_token(self):
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            session_ttl_seconds=30,
            client_auth_mode="per_session",
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            with pytest.raises(websockets.exceptions.InvalidHandshake):
                async with _connect(port, token=CLIENT_TOKEN):
                    pass
        finally:
            server.should_exit = True
            await task

    async def test_per_session_token_is_single_use(self):
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            session_ttl_seconds=30,
            client_auth_mode="per_session",
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            issued_token = app.state.client_token_store.issue(ttl_seconds=30)
            async with _connect(port, token=issued_token) as ws:
                await ws.send(
                    json.dumps({"type": "register", "os": "linux", "hostname": "h", "version": "1"})
                )
                await ws.recv()

            with pytest.raises(websockets.exceptions.InvalidHandshake):
                async with _connect(port, token=issued_token):
                    pass
        finally:
            server.should_exit = True
            await task


class TestWebSocketRegistration:
    async def test_register_receives_nine_digit_code(self, running_app):
        _app, port = running_app
        async with _connect(port) as ws:
            await ws.send(
                json.dumps({"type": "register", "os": "linux", "hostname": "srv01", "version": "1.0"})
            )
            reply = json.loads(await ws.recv())
            assert reply["type"] == "registered"
            assert re.fullmatch(r"\d{9}", reply["session_code"])

    async def test_wrong_token_is_rejected(self, running_app):
        _app, port = running_app
        with pytest.raises(websockets.exceptions.InvalidHandshake):
            async with _connect(port, token="wrong-token"):
                pass

    async def test_missing_token_is_rejected(self, running_app):
        _app, port = running_app
        with pytest.raises(websockets.exceptions.InvalidHandshake):
            async with _connect(port, token=None):
                pass

    async def test_heartbeat_ack(self, running_app):
        _app, port = running_app
        async with _connect(port) as ws:
            await ws.send(json.dumps({"type": "register", "os": "linux", "hostname": "h", "version": "1"}))
            await ws.recv()  # registered
            await ws.send(json.dumps({"type": "heartbeat"}))
            reply = json.loads(await ws.recv())
            assert reply["type"] == "heartbeat_ack"


class TestDispatchCommandOverRealWebSocket:
    async def test_command_roundtrip_and_aggregation(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            await ws.send(
                json.dumps({"type": "register", "os": "linux", "hostname": "srv01", "version": "1.0"})
            )
            reply = json.loads(await ws.recv())
            code = reply["session_code"]

            async def fake_client_loop():
                message = json.loads(await ws.recv())
                assert message["type"] == "command"
                assert message["tool"] == "run_shell"
                assert message["params"] == {"command": "echo hello", "shell": "auto"}
                request_id = message["request_id"]
                await ws.send(
                    json.dumps(
                        {"type": "stream", "request_id": request_id, "stream": "stdout", "data": "hello\n"}
                    )
                )
                await ws.send(
                    json.dumps({"type": "result", "request_id": request_id, "exit_code": 0, "error": None})
                )

            client_task = asyncio.create_task(fake_client_loop())

            broker = app.state.broker
            chunks = []
            async for chunk in broker.dispatch_command(
                code, "run_shell", {"command": "echo hello", "shell": "auto"}, timeout=5
            ):
                chunks.append(chunk)

            await client_task
            assert chunks == [
                {"type": "stream", "stream": "stdout", "data": "hello\n"},
                {"type": "result", "exit_code": 0, "error": None, "meta": None},
            ]

    async def test_session_not_found_for_unknown_code(self, running_app):
        app, _port = running_app
        broker = app.state.broker
        with pytest.raises(SessionNotFoundError):
            async for _ in broker.dispatch_command("000000000", "run_command", {"command": "x"}):
                pass

    async def test_client_disconnect_mid_command_raises(self, running_app):
        app, port = running_app
        ws = await _connect(port)
        await ws.send(json.dumps({"type": "register", "os": "linux", "hostname": "h", "version": "1"}))
        reply = json.loads(await ws.recv())
        code = reply["session_code"]

        broker = app.state.broker

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}, timeout=5):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0.1)  # laisse la commande partir côté serveur
        await ws.close()

        with pytest.raises(ClientDisconnectedError):
            await asyncio.wait_for(task, timeout=5)


class TestLegacyClientWithoutCapabilities:
    """Non-régression cardinale : un client **déjà déployé** (aucun champ
    `capabilities` dans son `register`) reste pleinement utilisable face à un
    relay mis à jour, et les nouveaux outils fichiers lui répondent
    `unsupported_by_client` sans jamais lui envoyer de `command` inconnue.
    """

    async def test_session_without_capabilities_still_runs_shell_commands(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, hostname="old-client", version="0.1.0")

            record = await app.state.broker.get_session_info(code)
            assert record.capabilities == ()

            async def fake_client_loop():
                message = json.loads(await ws.recv())
                assert message["type"] == "command"
                assert message["tool"] == "run_shell"
                request_id = message["request_id"]
                await ws.send(
                    json.dumps(
                        {"type": "stream", "request_id": request_id, "stream": "stdout", "data": "hello\n"}
                    )
                )
                await ws.send(
                    json.dumps({"type": "result", "request_id": request_id, "exit_code": 0, "error": None})
                )

            client_task = asyncio.create_task(fake_client_loop())
            result = await _call_tool(
                app, "run_shell", {"session_code": code, "command": "echo hello", "timeout": 5}
            )
            await client_task

            assert result["status"] == "ok"
            assert result["stdout"] == "hello\n"
            assert result["exit_code"] == 0
            assert result["error"] is None
            assert "meta" not in result  # sortie d'outil de commande inchangée

    async def test_file_tools_refuse_without_sending_any_command(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, hostname="old-client", version="0.1.0")

            read = await _call_tool(app, "read_file", {"session_code": code, "path": "/etc/hosts"})
            assert read["status"] == "error"
            assert read["error"] == "unsupported_by_client"
            assert read["client_version"] == "0.1.0"
            assert read["capabilities"] == []

            written = await _call_tool(
                app,
                "write_file",
                {"session_code": code, "path": "/tmp/f", "content_base64": "aGk="},
            )
            assert written["error"] == "unsupported_by_client"

            # Aucun message n'a été poussé vers le client : il n'a donc jamais eu
            # à ignorer un `tool` qu'il ne connaît pas.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)

    async def test_connect_session_reports_no_capability(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, version="0.1.0")
            info = await _call_tool(app, "connect_session", {"session_code": code})
            assert info["status"] == "connected"
            assert info["capabilities"] == []


class TestCapabilityAwareClient:
    """Un client à jour déclare `capabilities` ; le relay le propage jusqu'au harnais."""

    async def test_declared_capabilities_are_visible_from_connect_session(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, version="0.2.0", capabilities=["file_transfer"])
            record = await app.state.broker.get_session_info(code)
            assert record.capabilities == ("file_transfer",)
            info = await _call_tool(app, "connect_session", {"session_code": code})
            assert info["capabilities"] == ["file_transfer"]

    async def test_read_file_reaches_a_capable_client(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, version="0.2.0", capabilities=["file_transfer"])

            async def fake_client_loop():
                message = json.loads(await ws.recv())
                assert message["type"] == "command"
                assert message["tool"] == "read_file"
                assert message["params"] == {"path": "/etc/hosts"}
                request_id = message["request_id"]
                await ws.send(
                    json.dumps(
                        {"type": "file_chunk", "request_id": request_id, "seq": 0, "data": "aGVsbG8="}
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "result",
                            "request_id": request_id,
                            "exit_code": 0,
                            "error": None,
                            "meta": {"path": "/etc/hosts", "size": 5, "sha256": "x", "truncated": False},
                        }
                    )
                )

            client_task = asyncio.create_task(fake_client_loop())
            result = await _call_tool(app, "read_file", {"session_code": code, "path": "/etc/hosts"})
            await client_task

            assert result["status"] == "ok"
            assert result["content_base64"] == "aGVsbG8="
            assert result["size"] == 5

    @pytest.mark.parametrize(
        "declared, expected",
        [
            ("file_transfer", ()),  # chaîne : séquence de caractères, jamais une capacité
            ({"file_transfer": True}, ()),
            (123, ()),
            ([1, "file_transfer", None], ("file_transfer",)),
        ],
    )
    async def test_malformed_capabilities_never_break_registration(
        self, running_app, declared, expected
    ):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, capabilities=declared)
            record = await app.state.broker.get_session_info(code)
            assert record.capabilities == expected


class TestDesiredCodeOverRealWebSocket:
    """`desired_code` optionnel de `register` (adresse stable, cf. docs/PROTOCOL.md) :
    honoré quand libre et syntaxiquement valide, repli sûr sinon — jamais de
    crash ni d'éviction d'une connexion déjà en place sur ce code.
    """

    async def test_desired_code_is_honored_when_free(self, running_app):
        _app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, desired_code="784123678")
            assert code == "784123678"

    async def test_desired_code_falls_back_when_already_taken(self, running_app):
        _app, port = running_app
        async with _connect(port) as first_ws:
            first_code = await _register(first_ws, desired_code="784123678")
            assert first_code == "784123678"

            async with _connect(port) as second_ws:
                second_code = await _register(second_ws, desired_code="784123678")
                assert second_code != "784123678"
                assert re.fullmatch(r"\d{9}", second_code)

    @pytest.mark.parametrize(
        "desired_code",
        [
            "12345",  # trop court
            "1234567890",  # trop long
            "12345678a",  # non numérique
            123456789,  # pas une str
            {"code": "784123678"},  # pas une str
            None,  # explicitement absent
        ],
    )
    async def test_malformed_desired_code_never_breaks_registration(self, running_app, desired_code):
        _app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, desired_code=desired_code)
            assert re.fullmatch(r"\d{9}", code)

    async def test_legacy_register_without_desired_code_field_is_unaffected(self, running_app):
        # Ancien client : aucun champ `desired_code` du tout dans le `register`.
        _app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws)
            assert re.fullmatch(r"\d{9}", code)


class TestFileChunkOverRealWebSocket:
    async def test_file_chunks_are_routed_to_the_pending_request(self, running_app):
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws, version="0.2.0", capabilities=["file_transfer"])

            async def fake_client_loop():
                message = json.loads(await ws.recv())
                request_id = message["request_id"]
                for seq, data in enumerate(("aGVs", "bG8=")):
                    await ws.send(
                        json.dumps(
                            {"type": "file_chunk", "request_id": request_id, "seq": seq, "data": data}
                        )
                    )
                await ws.send(
                    json.dumps({"type": "result", "request_id": request_id, "exit_code": 0, "error": None})
                )

            client_task = asyncio.create_task(fake_client_loop())
            chunks = []
            async for chunk in app.state.broker.dispatch_command(
                code, "read_file", {"path": "/etc/hosts"}, timeout=5
            ):
                chunks.append(chunk)
            await client_task

            assert chunks == [
                {"type": "file_chunk", "seq": 0, "data": "aGVs"},
                {"type": "file_chunk", "seq": 1, "data": "bG8="},
                {"type": "result", "exit_code": 0, "error": None, "meta": None},
            ]


class TestApprovalResponseOverRealWebSocket:
    async def test_approved_command_still_delivers_its_output(self, running_app):
        # Reproduit le flot du client Go sous `policy=confirm` : approbation
        # émise *avant* l'exécution, puis stream/result.
        app, port = running_app
        async with _connect(port) as ws:
            code = await _register(ws)

            async def fake_client_loop():
                message = json.loads(await ws.recv())
                request_id = message["request_id"]
                await ws.send(
                    json.dumps(
                        {"type": "approval_response", "request_id": request_id, "approved": True}
                    )
                )
                await ws.send(
                    json.dumps(
                        {"type": "stream", "request_id": request_id, "stream": "stdout", "data": "ok\n"}
                    )
                )
                await ws.send(
                    json.dumps({"type": "result", "request_id": request_id, "exit_code": 0, "error": None})
                )

            client_task = asyncio.create_task(fake_client_loop())
            result = await _call_tool(
                app, "run_shell", {"session_code": code, "command": "whoami", "timeout": 5}
            )
            await client_task

            assert result["status"] == "ok"
            assert result["stdout"] == "ok\n"
            assert result["exit_code"] == 0
            assert result["error"] is None
