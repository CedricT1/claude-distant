"""Tests unitaires pour relay.broker : corrélation request_id et erreurs."""
import asyncio

import pytest

from relay.broker import Broker, ClientDisconnectedError, CommandTimeoutError, SessionNotFoundError
from relay.session_store import InMemorySessionStore


class FakeConnection:
    """Connexion client factice : capture les messages envoyés, sans réseau."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.fixture
def store():
    return InMemorySessionStore()


@pytest.fixture
def broker(store):
    return Broker(session_store=store, default_ttl_seconds=30, command_timeout=1)


class TestDispatchCommandHappyPath:
    async def test_aggregates_stream_then_result(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h1", version="1.0")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_shell", {"command": "echo hi"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)  # laisse dispatch_command envoyer la commande au client
        assert len(conn.sent) == 1
        sent = conn.sent[0]
        assert sent["type"] == "command"
        assert sent["tool"] == "run_shell"
        assert sent["params"] == {"command": "echo hi"}
        request_id = sent["request_id"]

        await broker.handle_client_message(
            conn, {"type": "stream", "request_id": request_id, "stream": "stdout", "data": "hi\n"}
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )

        chunks = await task
        assert chunks == [
            {"type": "stream", "stream": "stdout", "data": "hi\n"},
            {"type": "result", "exit_code": 0, "error": None},
        ]

    async def test_different_requests_do_not_cross_talk(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h1", version="1.0")

        async def run(command):
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": command}):
                chunks.append(chunk)
            return chunks

        task_a = asyncio.create_task(run("a"))
        await asyncio.sleep(0)
        task_b = asyncio.create_task(run("b"))
        await asyncio.sleep(0)

        assert len(conn.sent) == 2
        req_a = conn.sent[0]["request_id"]
        req_b = conn.sent[1]["request_id"]
        assert req_a != req_b

        # Répond à B d'abord, puis à A : chaque tâche ne doit recevoir que ses propres chunks.
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": req_b, "exit_code": 1, "error": None}
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": req_a, "exit_code": 0, "error": None}
        )

        result_a = await task_a
        result_b = await task_b
        assert result_a == [{"type": "result", "exit_code": 0, "error": None}]
        assert result_b == [{"type": "result", "exit_code": 1, "error": None}]


class TestDispatchCommandErrors:
    async def test_unknown_session_raises(self, broker):
        with pytest.raises(SessionNotFoundError):
            async for _ in broker.dispatch_command("000000000", "run_command", {"command": "x"}):
                pass

    async def test_expired_session_raises(self, broker, monkeypatch):
        conn = FakeConnection()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        fake_time[0] += 3600
        with pytest.raises(SessionNotFoundError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "x"}):
                pass

    async def test_client_disconnect_mid_command_raises(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        await broker.unregister_connection(conn)

        with pytest.raises(ClientDisconnectedError):
            await task

    async def test_timeout_when_no_response(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        with pytest.raises(CommandTimeoutError):
            async for _ in broker.dispatch_command(
                code, "run_command", {"command": "x"}, timeout=0.05
            ):
                pass


class TestHeartbeat:
    async def test_heartbeat_extends_ttl(self, broker, store, monkeypatch):
        conn = FakeConnection()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        fake_time[0] += 25  # proche de la fin du TTL par défaut (30s)
        assert await broker.heartbeat(code) is True
        fake_time[0] += 25  # dépasserait le TTL initial, mais le heartbeat l'a prolongé
        assert await store.get(code) is not None

    async def test_heartbeat_unknown_session_returns_false(self, broker):
        assert await broker.heartbeat("000000000") is False
