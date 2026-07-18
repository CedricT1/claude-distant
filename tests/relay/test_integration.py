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
                {"type": "result", "exit_code": 0, "error": None},
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
