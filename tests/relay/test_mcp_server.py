"""Tests unitaires pour relay.mcp_server : les 4 outils MCP, via le vrai SDK.

On instancie un vrai `FastMCP` (relay.mcp_server.create_mcp_server) et on
appelle les outils via l'API publique `mcp.call_tool(name, arguments)`, comme
le ferait un client MCP réel — mais le broker est remplacé par un double de
test (StubBroker) pour isoler la logique d'agrégation des outils du réseau
et du vrai broker (déjà testé dans test_broker.py).
"""
import json

from relay.broker import ClientDisconnectedError, CommandTimeoutError, SessionNotFoundError
from relay.mcp_server import create_mcp_server
from relay.session_store import SessionRecord


class StubBroker:
    """Double de test imitant l'API interne utilisée par mcp_server.py."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.session_info: SessionRecord | None = None
        self.chunks: list[dict] = []
        self.error: Exception | None = None

    async def get_session_info(self, session_code):
        self.calls.append(("get_session_info", session_code))
        return self.session_info

    async def dispatch_command(self, session_code, tool, params, timeout=None):
        self.calls.append(("dispatch_command", session_code, tool, dict(params), timeout))
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk


async def call_tool(mcp, name, arguments):
    """Appelle un outil via l'API publique du SDK et récupère son résultat structuré.

    Les outils sont annotés `-> dict[str, Any]` : le SDK détecte une sortie
    structurée et `call_tool` renvoie `(contenu_texte, dict_structuré)`. On
    retombe sur le parsing JSON du contenu texte si jamais ce n'est pas le cas.
    """
    result = await mcp.call_tool(name, arguments)
    if isinstance(result, tuple):
        _content, structured = result
        return structured
    return json.loads(result[0].text)


class TestConnectSession:
    async def test_returns_connected_status_for_known_session(self):
        broker = StubBroker()
        broker.session_info = SessionRecord(
            code="123456789",
            connection=object(),
            os="linux",
            hostname="srv01",
            version="1.0",
            created_at=0.0,
            expires_at=100.0,
        )
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "connect_session", {"session_code": "123456789"})
        assert result == {
            "status": "connected",
            "session_code": "123456789",
            "os": "linux",
            "hostname": "srv01",
            "version": "1.0",
        }

    async def test_returns_not_found_for_unknown_session(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "connect_session", {"session_code": "000000000"})
        assert result == {"status": "not_found", "session_code": "000000000"}


class TestRunCommandAggregation:
    async def test_aggregates_stdout_stderr_and_exit_code(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "stream", "stream": "stdout", "data": "line1\n"},
            {"type": "stream", "stream": "stderr", "data": "warn\n"},
            {"type": "stream", "stream": "stdout", "data": "line2\n"},
            {"type": "result", "exit_code": 0, "error": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "123456789", "command": "ls"})
        assert result == {
            "status": "ok",
            "stdout": "line1\nline2\n",
            "stderr": "warn\n",
            "exit_code": 0,
            "error": None,
        }
        assert broker.calls[-1][1:4] == ("123456789", "run_command", {"command": "ls"})

    async def test_passes_timeout_through(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker)
        await call_tool(mcp, "run_command", {"session_code": "1", "command": "ls", "timeout": 12})
        assert broker.calls[-1][4] == 12


class TestRunShell:
    async def test_defaults_shell_to_auto(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker)
        await call_tool(mcp, "run_shell", {"session_code": "1", "command": "echo hi"})
        assert broker.calls[-1][3]["shell"] == "auto"

    async def test_overrides_shell(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker)
        await call_tool(
            mcp, "run_shell", {"session_code": "1", "command": "dir", "shell": "powershell"}
        )
        assert broker.calls[-1][3]["shell"] == "powershell"


class TestErrorHandling:
    async def test_session_not_found_error(self):
        broker = StubBroker()
        broker.error = SessionNotFoundError("nope")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "0", "command": "x"})
        assert result["status"] == "error"
        assert result["error"] == "session_not_found"

    async def test_client_disconnected_error(self):
        broker = StubBroker()
        broker.error = ClientDisconnectedError("bye")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_shell", {"session_code": "0", "command": "x"})
        assert result["error"] == "client_disconnected"

    async def test_timeout_error(self):
        broker = StubBroker()
        broker.error = CommandTimeoutError("too slow")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "0", "command": "x"})
        assert result["error"] == "timeout"


class TestSystemInfo:
    async def test_calls_broker_with_system_info_tool(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "stream", "stream": "stdout", "data": "os=linux\n"},
            {"type": "result", "exit_code": 0, "error": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "system_info", {"session_code": "1"})
        assert result["stdout"] == "os=linux\n"
        assert broker.calls[-1][1:3] == ("1", "system_info")
