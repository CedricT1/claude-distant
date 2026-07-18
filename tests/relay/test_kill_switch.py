"""Tests pour le kill-switch de session : SessionStore.terminate, Broker.terminate_session,
et l'outil MCP `terminate_session`."""
import asyncio

import pytest

from relay.broker import Broker, ClientDisconnectedError
from relay.mcp_server import create_mcp_server
from relay.session_store import InMemorySessionStore


class FakeConnection:
    """Connexion client factice : capture messages envoyés, supporte close()."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def store():
    return InMemorySessionStore()


@pytest.fixture
def broker(store):
    return Broker(session_store=store, default_ttl_seconds=30, command_timeout=1)


class TestSessionStoreTerminate:
    async def test_terminate_removes_session(self, store):
        code = await store.create(connection="c", os="linux", hostname="h", version="1", ttl_seconds=30)
        record = await store.terminate(code)
        assert record is not None
        assert record.code == code
        assert await store.get(code) is None

    async def test_terminate_unknown_code_returns_none(self, store):
        assert await store.terminate("000000000") is None


class TestBrokerTerminateSession:
    async def test_terminate_invalidates_session(self, broker, store):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        assert await broker.terminate_session(code) is True
        assert await store.get(code) is None

    async def test_terminate_unknown_session_returns_false(self, broker):
        assert await broker.terminate_session("000000000") is False

    async def test_terminate_closes_client_connection(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        await broker.terminate_session(code)
        assert conn.closed is True

    async def test_terminate_fails_pending_command(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)  # laisse la commande partir
        await broker.terminate_session(code)

        with pytest.raises(ClientDisconnectedError):
            await task

    async def test_new_command_after_terminate_raises_session_not_found(self, broker):
        from relay.broker import SessionNotFoundError

        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        await broker.terminate_session(code)

        with pytest.raises(SessionNotFoundError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "x"}):
                pass


class StubBrokerForTermination:
    """Double de test pour l'outil MCP terminate_session."""

    def __init__(self) -> None:
        self.terminated_codes: list[str] = []
        self.result = True

    async def terminate_session(self, session_code: str) -> bool:
        self.terminated_codes.append(session_code)
        return self.result

    async def get_session_info(self, session_code):
        return None

    async def dispatch_command(self, session_code, tool, params, timeout=None):
        return
        yield  # pragma: no cover - jamais atteint, fait de ce double un générateur


class TestTerminateSessionMcpTool:
    async def test_tool_terminates_known_session(self):
        import json

        broker = StubBrokerForTermination()
        broker.result = True
        mcp = create_mcp_server(broker)
        result = await mcp.call_tool("terminate_session", {"session_code": "123456789"})
        structured = result[1] if isinstance(result, tuple) else json.loads(result[0].text)
        assert structured == {"status": "terminated", "session_code": "123456789"}
        assert broker.terminated_codes == ["123456789"]

    async def test_tool_reports_not_found_for_unknown_session(self):
        import json

        broker = StubBrokerForTermination()
        broker.result = False
        mcp = create_mcp_server(broker)
        result = await mcp.call_tool("terminate_session", {"session_code": "000000000"})
        structured = result[1] if isinstance(result, tuple) else json.loads(result[0].text)
        assert structured == {"status": "not_found", "session_code": "000000000"}
