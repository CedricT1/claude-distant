"""Tests unitaires pour relay.auth (validation des tokens Bearer)."""
import pytest

from relay.auth import (
    AuthError,
    PerSessionTokenStore,
    extract_bearer_token,
    require_bearer,
    verify_client_token,
    verify_token,
)


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


class TestPerSessionTokenStore:
    def test_issued_token_validates(self):
        store = PerSessionTokenStore()
        token = store.issue(ttl_seconds=30)
        assert store.validate(token) is True

    def test_unknown_token_does_not_validate(self):
        store = PerSessionTokenStore()
        assert store.validate("nope") is False

    def test_none_token_does_not_validate(self):
        store = PerSessionTokenStore()
        assert store.validate(None) is False

    def test_issued_tokens_are_unique(self):
        store = PerSessionTokenStore()
        tokens = {store.issue(ttl_seconds=30) for _ in range(20)}
        assert len(tokens) == 20

    def test_expired_token_does_not_validate(self):
        clock = [1000.0]
        store = PerSessionTokenStore(clock=lambda: clock[0])
        token = store.issue(ttl_seconds=5)
        clock[0] += 6
        assert store.validate(token) is False

    def test_consume_invalidates_token(self):
        store = PerSessionTokenStore()
        token = store.issue(ttl_seconds=30)
        store.consume(token)
        assert store.validate(token) is False

    def test_consuming_unknown_token_is_noop(self):
        store = PerSessionTokenStore()
        store.consume("nope")  # ne doit pas lever


class TestVerifyClientToken:
    def test_shared_mode_matches_existing_behavior(self):
        assert verify_client_token("secret", "shared", "secret", None) is True
        assert verify_client_token("wrong", "shared", "secret", None) is False

    def test_shared_mode_ignores_per_session_store(self):
        store = PerSessionTokenStore()
        # Même avec un store per-session fourni, le mode shared ne l'utilise pas.
        assert verify_client_token("secret", "shared", "secret", store) is True

    def test_per_session_mode_validates_against_store(self):
        store = PerSessionTokenStore()
        token = store.issue(ttl_seconds=30)
        assert verify_client_token(token, "per_session", "unused-shared-secret", store) is True

    def test_per_session_mode_rejects_shared_token(self):
        store = PerSessionTokenStore()
        store.issue(ttl_seconds=30)
        assert verify_client_token("unused-shared-secret", "per_session", "unused-shared-secret", store) is False

    def test_per_session_mode_without_store_denies_all(self):
        assert verify_client_token("anything", "per_session", "secret", None) is False
