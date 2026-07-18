"""Tests pour l'émetteur de jetons opérateur `python -m relay.tokens` (mode oauth)."""
import jwt
import pytest

from relay.tokens import issue_token, main

SECRET = "cli-test-secret"


class TestMainIssueCommand:
    def test_prints_a_valid_token(self, capsys):
        exit_code = main(["issue", "--sub", "operator-1", "--scopes", "session:connect,command:execute", "--ttl", "60", "--secret", SECRET])
        assert exit_code == 0
        printed = capsys.readouterr().out.strip()
        payload = jwt.decode(printed, SECRET, algorithms=["HS256"])
        assert payload["sub"] == "operator-1"
        assert payload["scopes"] == ["session:connect", "command:execute"]

    def test_splits_scopes_on_comma_and_trims_whitespace(self, capsys):
        main(["issue", "--sub", "op", "--scopes", " session:connect , command:execute ", "--ttl", "60", "--secret", SECRET])
        printed = capsys.readouterr().out.strip()
        payload = jwt.decode(printed, SECRET, algorithms=["HS256"])
        assert payload["scopes"] == ["session:connect", "command:execute"]

    def test_missing_secret_and_env_raises(self, monkeypatch):
        monkeypatch.delenv("MCP_JWT_SECRET", raising=False)
        with pytest.raises(ValueError):
            main(["issue", "--sub", "op", "--scopes", "session:connect", "--ttl", "60"])

    def test_reads_secret_from_env(self, monkeypatch, capsys):
        monkeypatch.setenv("MCP_JWT_SECRET", SECRET)
        exit_code = main(["issue", "--sub", "op", "--scopes", "session:connect", "--ttl", "60"])
        assert exit_code == 0
        printed = capsys.readouterr().out.strip()
        jwt.decode(printed, SECRET, algorithms=["HS256"])  # ne doit pas lever


class TestIssueTokenReexport:
    def test_reexports_issue_token_from_jwt_auth(self):
        token = issue_token("op", ["session:connect"], ttl_seconds=30, secret=SECRET)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == "op"
