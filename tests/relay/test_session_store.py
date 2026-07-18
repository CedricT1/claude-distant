"""Tests unitaires pour relay.session_store : génération de code et TTL."""
import re

from relay.session_store import InMemorySessionStore, generate_session_code


class TestGenerateSessionCode:
    def test_format_is_nine_digits(self):
        code = generate_session_code()
        assert re.fullmatch(r"\d{9}", code)

    def test_generates_varied_codes(self):
        codes = {generate_session_code() for _ in range(50)}
        assert len(codes) > 1  # extrêmement improbable d'obtenir toujours le même code


class TestInMemorySessionStoreCreate:
    async def test_create_returns_nine_digit_code(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1", os="linux", hostname="h1", version="1.0", ttl_seconds=30
        )
        assert re.fullmatch(r"\d{9}", code)

    async def test_create_retries_on_collision(self):
        store = InMemorySessionStore()
        first = await store.create(
            connection="conn-1", os="linux", hostname="h1", version="1.0", ttl_seconds=30
        )
        # Force une collision volontaire puis un code libre, pour vérifier
        # que le store retente jusqu'à obtenir un code non utilisé.
        codes_to_return = iter([first, "999999999"])
        store._code_generator = lambda: next(codes_to_return)
        second = await store.create(
            connection="conn-2", os="linux", hostname="h2", version="1.0", ttl_seconds=30
        )
        assert second == "999999999"
        assert second != first


class TestInMemorySessionStoreGet:
    async def test_get_returns_record_for_existing_code(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1", os="linux", hostname="h1", version="1.0", ttl_seconds=30
        )
        record = await store.get(code)
        assert record is not None
        assert record.connection == "conn-1"
        assert record.os == "linux"
        assert record.hostname == "h1"
        assert record.version == "1.0"
        assert record.code == code

    async def test_get_returns_none_for_unknown_code(self):
        store = InMemorySessionStore()
        assert await store.get("000000000") is None

    async def test_get_returns_none_after_ttl_expires(self, monkeypatch):
        store = InMemorySessionStore()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await store.create(connection="c", os="linux", hostname="h", version="1", ttl_seconds=5)
        fake_time[0] += 6
        assert await store.get(code) is None

    async def test_expired_entry_is_purged_from_store(self, monkeypatch):
        store = InMemorySessionStore()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await store.create(connection="c", os="linux", hostname="h", version="1", ttl_seconds=5)
        fake_time[0] += 6
        await store.get(code)  # déclenche l'expiration lazy
        assert code not in store._records


class TestInMemorySessionStoreTouch:
    async def test_touch_extends_ttl(self, monkeypatch):
        store = InMemorySessionStore()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await store.create(connection="c", os="linux", hostname="h", version="1", ttl_seconds=5)
        fake_time[0] += 3
        assert await store.touch(code, ttl_seconds=5) is True
        fake_time[0] += 4  # 7s depuis la création mais 4s depuis le touch : encore valide
        assert await store.get(code) is not None

    async def test_touch_unknown_code_returns_false(self):
        store = InMemorySessionStore()
        assert await store.touch("000000000", ttl_seconds=5) is False


class TestInMemorySessionStoreRemove:
    async def test_remove_deletes_code(self):
        store = InMemorySessionStore()
        code = await store.create(connection="c", os="linux", hostname="h", version="1", ttl_seconds=30)
        await store.remove(code)
        assert await store.get(code) is None

    async def test_remove_by_connection(self):
        store = InMemorySessionStore()
        code = await store.create(connection="conn-x", os="linux", hostname="h", version="1", ttl_seconds=30)
        await store.remove_by_connection("conn-x")
        assert await store.get(code) is None

    async def test_remove_unknown_code_is_noop(self):
        store = InMemorySessionStore()
        await store.remove("000000000")
