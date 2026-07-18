"""Test bout-en-bout réel, mode `MCP_AUTH_MODE=oauth`.

Même topologie que `test_e2e_static_bearer.py` (relay réel + vrai binaire
client Go + vrai client MCP), mais le harnais présente un **JWT scopé**
(HS256, émis via `relay.jwt_auth.issue_token` — l'équivalent programmatique
de `python -m relay.tokens issue`, cf. `relay/tokens.py`) au lieu d'un jeton
statique unique.

Couvre en plus le cas négatif attendu par `docs/PROTOCOL.md` §2 : un jeton
sans le scope `command:execute` doit pouvoir `connect_session` (scope
`session:connect` uniquement) mais se voit refuser `run_shell` avec
`forbidden_scope`.
"""
from __future__ import annotations

import pytest

from harness import CLIENT_TOKEN, RunningClient, RunningRelay, call_tool, mcp_client_session
from relay.jwt_auth import issue_token

pytestmark = pytest.mark.e2e

JWT_SECRET = "e2e-oauth-secret-at-least-32-bytes-long-ok"


async def test_oauth_full_roundtrip_and_scope_enforcement(client_binary):
    async with RunningRelay(
        client_token=CLIENT_TOKEN,
        mcp_bearer_token="unused-in-oauth-mode",
        session_ttl_seconds=60,
        mcp_auth_mode="oauth",
        mcp_jwt_secret=JWT_SECRET,
    ) as relay:
        async with RunningClient(client_binary, relay.ws_url, CLIENT_TOKEN) as client:
            session_code = await client.read_session_code()

            full_token = issue_token(
                "harness-e2e-full",
                ["session:connect", "command:execute", "session:terminate"],
                ttl_seconds=300,
                secret=JWT_SECRET,
            )
            restricted_token = issue_token(
                "harness-e2e-restricted",
                ["session:connect"],  # pas de command:execute
                ttl_seconds=300,
                secret=JWT_SECRET,
            )

            # --- Cas négatif : jeton sans command:execute -------------------
            async with mcp_client_session(relay.mcp_url, restricted_token) as mcp:
                connect = await call_tool(mcp, "connect_session", {"session_code": session_code})
                assert connect["status"] == "connected"  # session:connect présent, OK

                denied = await call_tool(
                    mcp,
                    "run_shell",
                    {"session_code": session_code, "command": "echo integration-ok"},
                )
                assert denied["status"] == "error"
                assert denied["error"] == "forbidden_scope"

            # --- Cas nominal : jeton complet, bout-en-bout réel --------------
            async with mcp_client_session(relay.mcp_url, full_token) as mcp:
                shell_result = await call_tool(
                    mcp,
                    "run_shell",
                    {"session_code": session_code, "command": "echo integration-ok", "shell": "auto"},
                )
                assert shell_result["status"] == "ok"
                assert shell_result["exit_code"] == 0
                assert "integration-ok" in shell_result["stdout"]

                cmd_result = await call_tool(
                    mcp, "run_command", {"session_code": session_code, "command": "true"}
                )
                assert cmd_result["status"] == "ok"
                assert cmd_result["exit_code"] == 0

                terminate = await call_tool(mcp, "terminate_session", {"session_code": session_code})
                assert terminate == {"status": "terminated", "session_code": session_code}


async def test_oauth_missing_token_rejected_at_transport(client_binary):
    """Un jeton absent est rejeté au niveau transport (401), avant tout appel
    d'outil — cf. `docs/PROTOCOL.md` §2 et `RequireAuthMiddleware` du SDK MCP."""
    import httpx

    async with RunningRelay(
        client_token=CLIENT_TOKEN,
        mcp_bearer_token="unused-in-oauth-mode",
        session_ttl_seconds=60,
        mcp_auth_mode="oauth",
        mcp_jwt_secret=JWT_SECRET,
    ) as relay:
        async with httpx.AsyncClient() as http_client:
            r = await http_client.post(
                relay.mcp_url,
                json={},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert r.status_code == 401
