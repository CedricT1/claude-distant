"""Tests unitaires pour relay.broker : corrélation request_id et erreurs."""
import asyncio

import pytest

from relay.broker import (
    Broker,
    ClientDisconnectedError,
    CommandDeniedError,
    CommandTimeoutError,
    SessionNotFoundError,
)
from relay.command_policy import CommandPolicy
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


class FakeAuditLog:
    """Double de test pour AuditLog : capture les événements sans toucher au disque."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> dict:
        self.events.append(event)
        return event


class TestCommandPolicyWiring:
    async def test_denied_command_never_reaches_client(self, store):
        conn = FakeConnection()
        policy = CommandPolicy(denylist=["rm -rf"])
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                         command_policy=policy, audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        with pytest.raises(CommandDeniedError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "rm -rf /"}):
                pass

        assert conn.sent == []  # jamais envoyée au client

    async def test_denied_command_is_audited(self, store):
        conn = FakeConnection()
        policy = CommandPolicy(denylist=["rm -rf"])
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                         command_policy=policy, audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        with pytest.raises(CommandDeniedError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "rm -rf /"}):
                pass

        assert len(audit.events) == 1
        assert audit.events[0]["decision"] == "denied"
        assert audit.events[0]["session_code"] == code
        assert audit.events[0]["tool"] == "run_command"

    async def test_allowed_command_is_audited_with_outcome(self, store):
        conn = FakeConnection()
        policy = CommandPolicy()  # permissive
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                         command_policy=policy, audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            async for _ in broker.dispatch_command(code, "run_command", {"command": "echo hi"}):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        await task

        assert len(audit.events) == 1
        assert audit.events[0]["decision"] == "allowed"
        assert audit.events[0]["outcome"] == {"exit_code": 0, "error": None}

    async def test_without_policy_or_audit_behaves_as_before(self, broker):
        # `broker` (fixture) est construit sans command_policy/audit_log : aucune
        # régression pour les usages existants du MVP.
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        chunks = await task
        assert chunks == [{"type": "result", "exit_code": 0, "error": None}]


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
