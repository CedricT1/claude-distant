"""Tests unitaires pour relay.jwt_auth : émission et vérification de jetons Bearer JWT
(mode `MCP_AUTH_MODE=oauth`, phase 5 — durcissement sécurité).

`issue_token` signe un JWT HS256 avec `sub`/`exp`/`scopes` ; `JWTTokenVerifier`
implémente le protocole `mcp.server.auth.provider.TokenVerifier` attendu par le
SDK MCP (`async def verify_token(self, token: str) -> AccessToken | None`).
"""
import time

import jwt
import pytest

from relay.jwt_auth import JWTTokenVerifier, issue_token

SECRET = "test-jwt-secret"


class TestIssueToken:
    def test_issues_a_decodable_jwt(self):
        token = issue_token("operator-1", ["session:connect"], ttl_seconds=60, secret=SECRET)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == "operator-1"
        assert payload["scopes"] == ["session:connect"]
        assert "exp" in payload

    def test_exp_reflects_ttl(self):
        now = 1_000_000.0
        token = issue_token("op", [], ttl_seconds=100, secret=SECRET, now=now)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
        assert payload["exp"] == int(now) + 100

    def test_missing_secret_raises(self, monkeypatch):
        monkeypatch.delenv("MCP_JWT_SECRET", raising=False)
        with pytest.raises(ValueError):
            issue_token("op", ["session:connect"], ttl_seconds=60, secret=None)

    def test_secret_read_from_env_when_not_passed(self, monkeypatch):
        monkeypatch.setenv("MCP_JWT_SECRET", SECRET)
        token = issue_token("op", ["session:connect"], ttl_seconds=60, secret=None)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == "op"


class TestJWTTokenVerifier:
    async def test_valid_token_returns_access_token(self):
        token = issue_token("harness-1", ["session:connect", "command:execute"], ttl_seconds=60, secret=SECRET)
        verifier = JWTTokenVerifier(secret=SECRET)
        access = await verifier.verify_token(token)
        assert access is not None
        assert access.client_id == "harness-1"
        assert access.subject == "harness-1"
        assert set(access.scopes) == {"session:connect", "command:execute"}
        assert access.expires_at is not None

    async def test_expired_token_returns_none(self):
        token = issue_token("op", ["session:connect"], ttl_seconds=-10, secret=SECRET)
        verifier = JWTTokenVerifier(secret=SECRET)
        assert await verifier.verify_token(token) is None

    async def test_bad_signature_returns_none(self):
        token = issue_token("op", ["session:connect"], ttl_seconds=60, secret="wrong-secret")
        verifier = JWTTokenVerifier(secret=SECRET)
        assert await verifier.verify_token(token) is None

    async def test_garbage_token_returns_none(self):
        verifier = JWTTokenVerifier(secret=SECRET)
        assert await verifier.verify_token("not-a-jwt") is None

    async def test_missing_sub_returns_none(self):
        payload = {"exp": int(time.time()) + 60, "scopes": ["session:connect"]}
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        verifier = JWTTokenVerifier(secret=SECRET)
        assert await verifier.verify_token(token) is None

    async def test_missing_scopes_defaults_to_empty_list(self):
        payload = {"sub": "op", "exp": int(time.time()) + 60}
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        verifier = JWTTokenVerifier(secret=SECRET)
        access = await verifier.verify_token(token)
        assert access is not None
        assert access.scopes == []

    async def test_wrong_algorithm_is_rejected(self):
        # Le verifier n'accepte que HS256 par défaut : un jeton signé avec un
        # autre algorithme (ou "none") ne doit jamais être accepté.
        token = jwt.encode(
            {"sub": "op", "exp": int(time.time()) + 60, "scopes": []}, "", algorithm="none"
        )
        verifier = JWTTokenVerifier(secret=SECRET)
        assert await verifier.verify_token(token) is None
