"""Tests de câblage `relay.app.create_app` pour `MCP_AUTH_MODE` (phase 5) : le mode
oauth est branché de bout en bout (endpoint `/mcp` réel, `.well-known` RFC 9728 à la
racine), et `issue_client_token` (outil MCP) permet d'obtenir un jeton client
`per_session` utilisable pour un vrai handshake WebSocket — sans plus jamais avoir à
appeler `PerSessionTokenStore.issue(...)` directement côté déploiement.
"""
import asyncio
import json

import httpx
import pytest
import uvicorn
import websockets

from relay.app import create_app
from relay.jwt_auth import issue_token

CLIENT_TOKEN = "test-client-token"
MCP_TOKEN = "test-mcp-token"
JWT_SECRET = "test-jwt-secret-for-app-wiring"


class TestMcpEndpointStaticBearerDefault:
    async def test_default_mode_requires_the_shared_bearer_token(self):
        app = create_app(client_token=CLIENT_TOKEN, mcp_bearer_token=MCP_TOKEN)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401
            async with app.state.mcp.session_manager.run():
                r = await client.post(
                    "/mcp",
                    json={},
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {MCP_TOKEN}",
                    },
                )
                assert r.status_code != 401


class TestMcpEndpointOAuthMode:
    async def test_oauth_mode_wires_real_mcp_path_without_double_mount(self):
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            mcp_auth_mode="oauth",
            mcp_jwt_secret=JWT_SECRET,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401

    async def test_well_known_protected_resource_metadata_is_at_root(self):
        # RFC 9728 : les métadonnées de ressource protégée doivent être servies à la
        # racine du serveur de ressource, pas sous le préfixe applicatif (`/mcp`).
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            mcp_auth_mode="oauth",
            mcp_jwt_secret=JWT_SECRET,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert r.status_code == 200

    async def test_invalid_jwt_signature_is_rejected(self):
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            mcp_auth_mode="oauth",
            mcp_jwt_secret=JWT_SECRET,
        )
        bad_token = issue_token("harness-1", ["session:connect"], ttl_seconds=60, secret="not-the-right-secret")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/mcp",
                json={},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {bad_token}",
                },
            )
            assert r.status_code == 401
            healthz = await client.get("/healthz")
            assert healthz.status_code == 200


class TestIssueClientTokenEndToEndWithRealSocket:
    async def test_token_issued_by_mcp_tool_authenticates_a_real_ws_client(self):
        app = create_app(
            client_token=CLIENT_TOKEN,
            mcp_bearer_token=MCP_TOKEN,
            client_auth_mode="per_session",
            session_ttl_seconds=30,
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            mcp = app.state.mcp
            result = await mcp.call_tool("issue_client_token", {"ttl_seconds": 30})
            structured = result[1] if isinstance(result, tuple) else json.loads(result[0].text)
            assert structured["status"] == "ok"
            issued = structured["token"]

            headers = {"Authorization": f"Bearer {issued}"}
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws/client", additional_headers=headers, proxy=None
            ) as ws:
                await ws.send(json.dumps({"type": "register", "os": "linux", "hostname": "h", "version": "1"}))
                reply = json.loads(await ws.recv())
                assert reply["type"] == "registered"
        finally:
            server.should_exit = True
            await task
