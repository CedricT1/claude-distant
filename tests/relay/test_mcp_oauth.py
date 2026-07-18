"""Tests pour le mode `MCP_AUTH_MODE=oauth` : enforcement des scopes par outil MCP,
émission de jeton client (`issue_client_token`), et câblage transport réel du SDK
(`mcp.server.auth`) via `relay.mcp_server.build_mcp_asgi_app`.

Le mode `oauth` reste auto-contenu (HS256, secret partagé `MCP_JWT_SECRET`) : pas
d'infrastructure OAuth externe requise pour ces tests, cf. `relay/jwt_auth.py`.
"""
import json
from contextlib import contextmanager

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from relay.audit import AuditLog
from relay.auth import PerSessionTokenStore
from relay.jwt_auth import issue_token
from relay.mcp_server import build_mcp_asgi_app, create_mcp_server
from relay.session_store import SessionRecord

SECRET = "oauth-test-secret"


class StubBroker:
    """Double de test imitant l'API interne utilisée par mcp_server.py (cf. test_mcp_server.py)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.session_info: SessionRecord | None = None
        self.chunks: list[dict] = []
        self.terminated_codes: list[str] = []
        self.terminate_result = True

    async def get_session_info(self, session_code):
        self.calls.append(("get_session_info", session_code))
        return self.session_info

    async def dispatch_command(self, session_code, tool, params, timeout=None):
        self.calls.append(("dispatch_command", session_code, tool, dict(params), timeout))
        for chunk in self.chunks:
            yield chunk

    async def terminate_session(self, session_code: str) -> bool:
        self.terminated_codes.append(session_code)
        return self.terminate_result


async def call_tool(mcp, name, arguments):
    result = await mcp.call_tool(name, arguments)
    if isinstance(result, tuple):
        _content, structured = result
        return structured
    return json.loads(result[0].text)


@contextmanager
def as_principal(scopes, client_id="harness-1"):
    """Simule un appel MCP authentifié : place un `AccessToken` dans le contextvar
    lu par `mcp.server.auth.middleware.auth_context.get_access_token()`, exactement
    comme le ferait `AuthContextMiddleware` pour une vraie requête HTTP passée par
    `BearerAuthBackend`."""
    token = AccessToken(token="t", client_id=client_id, scopes=list(scopes), expires_at=None, subject=client_id)
    ctxtoken = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(ctxtoken)


@contextmanager
def as_anonymous():
    ctxtoken = auth_context_var.set(None)
    try:
        yield
    finally:
        auth_context_var.reset(ctxtoken)


class TestScopeEnforcementDisabledByDefault:
    """`require_scopes=False` (défaut) : comportement historique du MVP inchangé,
    même sans aucun contexte d'authentification (cas des tests existants)."""

    async def test_connect_session_works_without_any_auth_context(self):
        broker = StubBroker()
        broker.session_info = SessionRecord(
            code="123456789", connection=object(), os="linux", hostname="h", version="1", created_at=0.0, expires_at=100.0
        )
        mcp = create_mcp_server(broker)
        with as_anonymous():
            result = await call_tool(mcp, "connect_session", {"session_code": "123456789"})
        assert result["status"] == "connected"


class TestScopeEnforcementConnectSession:
    async def test_denied_without_scope(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["command:execute"]):
            result = await call_tool(mcp, "connect_session", {"session_code": "1"})
        assert result["status"] == "error"
        assert result["error"] == "forbidden_scope"

    async def test_denied_with_no_token_at_all(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_anonymous():
            result = await call_tool(mcp, "connect_session", {"session_code": "1"})
        assert result["status"] == "error"
        assert result["error"] == "forbidden_scope"

    async def test_allowed_with_scope(self):
        broker = StubBroker()
        broker.session_info = SessionRecord(
            code="1", connection=object(), os="linux", hostname="h", version="1", created_at=0.0, expires_at=100.0
        )
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["session:connect"]):
            result = await call_tool(mcp, "connect_session", {"session_code": "1"})
        assert result["status"] == "connected"


class TestScopeEnforcementRunCommandAndRunShell:
    async def test_run_command_denied_without_scope(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["session:connect"]):
            result = await call_tool(mcp, "run_command", {"session_code": "1", "command": "ls"})
        assert result == {
            "status": "error",
            "error": "forbidden_scope",
            "detail": "le jeton ne porte pas le scope requis : 'command:execute'",
        }
        assert broker.calls == []  # jamais dispatché au broker

    async def test_run_command_allowed_with_scope(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["command:execute"]):
            result = await call_tool(mcp, "run_command", {"session_code": "1", "command": "ls"})
        assert result["status"] == "ok"

    async def test_run_shell_denied_without_scope(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal([]):
            result = await call_tool(mcp, "run_shell", {"session_code": "1", "command": "echo hi"})
        assert result["error"] == "forbidden_scope"

    async def test_run_shell_allowed_with_scope(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["command:execute"]):
            result = await call_tool(mcp, "run_shell", {"session_code": "1", "command": "echo hi"})
        assert result["status"] == "ok"


class TestScopeEnforcementTerminateSession:
    async def test_denied_without_scope(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["command:execute"]):
            result = await call_tool(mcp, "terminate_session", {"session_code": "1"})
        assert result["error"] == "forbidden_scope"
        assert broker.terminated_codes == []

    async def test_allowed_with_scope(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["session:terminate"]):
            result = await call_tool(mcp, "terminate_session", {"session_code": "1"})
        assert result == {"status": "terminated", "session_code": "1"}
        assert broker.terminated_codes == ["1"]


class TestScopeDenialIsAudited:
    async def test_denied_call_is_recorded_in_audit_log(self, tmp_path):
        log_path = tmp_path / "audit.log"
        audit_log = AuditLog(path=log_path)
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True, audit_log=audit_log)
        with as_principal([]):
            await call_tool(mcp, "run_command", {"session_code": "42", "command": "rm -rf /"})
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["decision"] == "denied"
        assert entry["tool"] == "run_command"
        assert "command:execute" in entry["outcome"]["reason"]


class TestIssueClientTokenTool:
    async def test_issues_token_that_validates_against_store(self):
        store = PerSessionTokenStore()
        broker = StubBroker()
        mcp = create_mcp_server(broker, client_token_store=store)
        result = await call_tool(mcp, "issue_client_token", {"ttl_seconds": 30})
        assert result["status"] == "ok"
        assert store.validate(result["token"]) is True

    async def test_default_ttl_used_when_not_specified(self):
        store = PerSessionTokenStore()
        broker = StubBroker()
        mcp = create_mcp_server(broker, client_token_store=store, client_token_ttl_seconds=45.0)
        result = await call_tool(mcp, "issue_client_token", {})
        assert result["expires_in"] == 45.0

    async def test_without_store_configured_returns_error(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker, client_token_store=None)
        result = await call_tool(mcp, "issue_client_token", {})
        assert result["status"] == "error"
        assert result["error"] == "not_configured"

    async def test_requires_client_provision_scope_in_oauth_mode(self):
        store = PerSessionTokenStore()
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True, client_token_store=store)
        with as_principal(["session:connect"]):
            result = await call_tool(mcp, "issue_client_token", {})
        assert result["error"] == "forbidden_scope"

    async def test_allowed_with_client_provision_scope(self):
        store = PerSessionTokenStore()
        broker = StubBroker()
        mcp = create_mcp_server(broker, require_scopes=True, client_token_store=store)
        with as_principal(["client:provision"]):
            result = await call_tool(mcp, "issue_client_token", {})
        assert result["status"] == "ok"
        assert store.validate(result["token"]) is True


class TestBuildMcpAsgiAppStaticBearerModeUnchanged:
    async def test_static_bearer_mode_still_requires_bearer_token(self):
        broker = StubBroker()
        _mcp, asgi_app = build_mcp_asgi_app(broker, mode="static_bearer", bearer_token="secret-token")
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401

    async def test_static_bearer_mode_is_the_default(self):
        broker = StubBroker()
        mcp, asgi_app = build_mcp_asgi_app(broker, bearer_token="secret-token")
        transport = httpx.ASGITransport(app=asgi_app)
        async with mcp.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/mcp",
                    json={},
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Authorization": "Bearer secret-token",
                    },
                )
                assert r.status_code != 401


class TestBuildMcpAsgiAppOAuthModeTransport:
    async def test_rejects_missing_token(self):
        broker = StubBroker()
        _mcp, asgi_app = build_mcp_asgi_app(broker, mode="oauth", jwt_secret=SECRET)
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401
            assert "Bearer" in r.headers.get("www-authenticate", "")

    async def test_rejects_invalid_signature(self):
        broker = StubBroker()
        _mcp, asgi_app = build_mcp_asgi_app(broker, mode="oauth", jwt_secret=SECRET)
        bad_token = issue_token("harness", ["session:connect"], ttl_seconds=60, secret="wrong-secret")
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/mcp",
                json={},
                headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {bad_token}"},
            )
            assert r.status_code == 401

    async def test_rejects_expired_token(self):
        broker = StubBroker()
        _mcp, asgi_app = build_mcp_asgi_app(broker, mode="oauth", jwt_secret=SECRET)
        expired = issue_token("harness", ["session:connect"], ttl_seconds=-10, secret=SECRET)
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/mcp",
                json={},
                headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {expired}"},
            )
            assert r.status_code == 401

    async def test_accepts_valid_token_past_the_auth_layer(self):
        broker = StubBroker()
        mcp, asgi_app = build_mcp_asgi_app(broker, mode="oauth", jwt_secret=SECRET)
        good = issue_token("harness", ["session:connect"], ttl_seconds=60, secret=SECRET)
        transport = httpx.ASGITransport(app=asgi_app)
        async with mcp.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"},
                        },
                    },
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {good}",
                    },
                )
                assert r.status_code == 200
