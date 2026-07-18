"""Tests unitaires pour relay.auth (validation des tokens Bearer)."""
import pytest

from relay.auth import AuthError, extract_bearer_token, require_bearer, verify_token


class TestExtractBearerToken:
    def test_valid_header(self):
        assert extract_bearer_token("Bearer abc123") == "abc123"

    def test_missing_header(self):
        assert extract_bearer_token(None) is None

    def test_wrong_scheme(self):
        assert extract_bearer_token("Basic abc123") is None

    def test_empty_token_after_scheme(self):
        assert extract_bearer_token("Bearer ") is None

    def test_malformed_header_no_scheme(self):
        assert extract_bearer_token("abc123") is None

    def test_case_insensitive_scheme(self):
        assert extract_bearer_token("bearer abc123") == "abc123"


class TestVerifyToken:
    def test_matching_tokens(self):
        assert verify_token("secret", "secret") is True

    def test_mismatched_tokens(self):
        assert verify_token("secret", "other") is False

    def test_none_provided(self):
        assert verify_token(None, "secret") is False

    def test_empty_expected_denies_all(self):
        # Un token attendu vide (mauvaise config) ne doit jamais matcher.
        assert verify_token("", "") is False
        assert verify_token(None, None) is False


class TestRequireBearer:
    def test_valid_passes_silently(self):
        require_bearer("Bearer secret", "secret")

    def test_invalid_raises_auth_error(self):
        with pytest.raises(AuthError):
            require_bearer("Bearer wrong", "secret")

    def test_missing_header_raises_auth_error(self):
        with pytest.raises(AuthError):
            require_bearer(None, "secret")
