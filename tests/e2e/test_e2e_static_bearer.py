"""Test bout-en-bout réel, mode `MCP_AUTH_MODE=static_bearer` (défaut).

Relie les trois composants réels décrits dans `docs/PROTOCOL.md` :
1. le relay (`relay.app.create_app`, vrai `uvicorn.Server` sur un port
   éphémère réel) ;
2. le vrai binaire client Go compilé (`client/`), en sous-processus réel,
   qui se connecte en sortant au relay et exécute réellement les commandes ;
3. un vrai client MCP (SDK officiel, transport Streamable HTTP) qui joue le
   rôle du harnais, authentifié par `Authorization: Bearer <MCP_BEARER_TOKEN>`.

Aucun composant n'est mocké : `run_shell`/`run_command` exécutent une vraie
commande shell sur cette machine (via le vrai executor Go), et leur sortie
réelle remonte jusqu'à l'assertion.
"""
from __future__ import annotations

import pytest

from harness import CLIENT_TOKEN, STATIC_MCP_TOKEN, RunningClient, RunningRelay, call_tool, mcp_client_session

pytestmark = pytest.mark.e2e


async def test_full_roundtrip_static_bearer(client_binary):
    async with RunningRelay(
        client_token=CLIENT_TOKEN,
        mcp_bearer_token=STATIC_MCP_TOKEN,
        session_ttl_seconds=60,
    ) as relay:
        async with RunningClient(client_binary, relay.ws_url, CLIENT_TOKEN) as client:
            session_code = await client.read_session_code()
            assert len(session_code) == 9 and session_code.isdigit()

            async with mcp_client_session(relay.mcp_url, STATIC_MCP_TOKEN) as mcp:
                # 1. connect_session : statut + OS/hostname réels remontés par
                #    le vrai process client (via son message `register`).
                connect = await call_tool(mcp, "connect_session", {"session_code": session_code})
                assert connect["status"] == "connected"
                assert connect["session_code"] == session_code
                assert connect["os"] == "linux"
                assert connect["hostname"]  # non vide

                # 2. run_shell : sortie réelle d'une vraie commande shell.
                shell_result = await call_tool(
                    mcp,
                    "run_shell",
                    {"session_code": session_code, "command": "echo integration-ok", "shell": "auto"},
                )
                assert shell_result["status"] == "ok"
                assert shell_result["exit_code"] == 0
                assert "integration-ok" in shell_result["stdout"]

                # 3. run_command : exécution directe (sans shell) d'un binaire simple.
                cmd_result = await call_tool(
                    mcp, "run_command", {"session_code": session_code, "command": "true"}
                )
                assert cmd_result["status"] == "ok"
                assert cmd_result["exit_code"] == 0

                # 4. terminate_session : kill-switch réel.
                terminate = await call_tool(mcp, "terminate_session", {"session_code": session_code})
                assert terminate == {"status": "terminated", "session_code": session_code}

                after = await call_tool(mcp, "connect_session", {"session_code": session_code})
                assert after["status"] == "not_found"


async def test_wrong_mcp_bearer_token_is_rejected(client_binary):
    """Vérifie, sur le vrai transport HTTP, qu'un jeton Bearer incorrect est
    rejeté avant même d'atteindre un outil (401, cf. `BearerAuthMiddleware`)."""
    import httpx

    async with RunningRelay(
        client_token=CLIENT_TOKEN,
        mcp_bearer_token=STATIC_MCP_TOKEN,
        session_ttl_seconds=60,
    ) as relay:
        async with httpx.AsyncClient() as http_client:
            r = await http_client.post(
                relay.mcp_url,
                json={},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer wrong-token",
                },
            )
            assert r.status_code == 401
