"""Tests unitaires pour relay.session_store : génération de code et TTL."""
import re

import pytest

from relay.session_store import InMemorySessionStore, SessionRecord, generate_session_code


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


class TestInMemorySessionStoreDesiredCode:
    """Code de session souhaité (adresse stable, cf. docs/PROTOCOL.md) : honoré
    quand il est syntaxiquement valide et libre, sinon repli sur le tirage
    aléatoire existant. Premier arrivé, premier servi — voir la docstring de
    `InMemorySessionStore.create` pour le choix (assumé) de ne jamais évincer
    une connexion déjà en place sur ce code.
    """

    async def test_desired_code_is_honored_when_free(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1",
            os="linux",
            hostname="h1",
            version="1.0",
            ttl_seconds=30,
            desired_code="784123678",
        )
        assert code == "784123678"

    async def test_desired_code_falls_back_to_random_when_already_taken(self):
        store = InMemorySessionStore()
        first = await store.create(
            connection="conn-1",
            os="linux",
            hostname="h1",
            version="1.0",
            ttl_seconds=30,
            desired_code="111111111",
        )
        assert first == "111111111"
        # Générateur de repli forcé : sans ça on ne pourrait vérifier que
        # `second != desired_code`, ce qui resterait vrai même en cas de bug
        # (ex. collision fortuite avec le tirage aléatoire réel).
        store._code_generator = lambda: "222222222"
        second = await store.create(
            connection="conn-2",
            os="linux",
            hostname="h2",
            version="1.0",
            ttl_seconds=30,
            desired_code="111111111",
        )
        assert second == "222222222"

    @pytest.mark.parametrize(
        "desired_code",
        [
            "12345",  # trop court
            "1234567890",  # trop long
            "12345678a",  # non numérique
            123456789,  # pas une str
            None,  # explicitement absent
        ],
    )
    async def test_desired_code_falls_back_when_malformed(self, desired_code):
        store = InMemorySessionStore()
        store._code_generator = lambda: "333333333"
        code = await store.create(
            connection="conn-1",
            os="linux",
            hostname="h1",
            version="1.0",
            ttl_seconds=30,
            desired_code=desired_code,
        )
        assert code == "333333333"

    async def test_create_without_desired_code_argument_is_unaffected(self):
        # Appelant historique : le paramètre n'existe pas dans son vocabulaire,
        # le comportement (génération aléatoire) doit rester identique.
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1", os="linux", hostname="h1", version="1.0", ttl_seconds=30
        )
        assert re.fullmatch(r"\d{9}", code)


class TestInMemorySessionStoreCapabilities:
    """Négociation de capacités : champ additif, optionnel, normalisé en tuple.

    Un ancien client ne déclare aucune capacité : le store doit alors retenir
    `()` — c'est la valeur qui garantit qu'aucun outil « nouvelle génération »
    ne lui sera jamais dispatché (cf. `_require_capability` côté MCP).
    """

    async def test_capabilities_default_to_empty_tuple(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1", os="linux", hostname="h1", version="0.1.0", ttl_seconds=30
        )
        record = await store.get(code)
        assert record.capabilities == ()

    async def test_capabilities_are_stored_when_provided(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1",
            os="linux",
            hostname="h1",
            version="0.2.0",
            ttl_seconds=30,
            capabilities=["file_transfer"],
        )
        record = await store.get(code)
        assert record.capabilities == ("file_transfer",)

    async def test_capabilities_are_normalised_to_a_tuple(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1",
            os="linux",
            hostname="h1",
            version="0.2.0",
            ttl_seconds=30,
            capabilities=["file_transfer", "future_thing"],
        )
        record = await store.get(code)
        assert isinstance(record.capabilities, tuple)
        assert record.capabilities == ("file_transfer", "future_thing")

    async def test_explicit_none_is_treated_as_no_capability(self):
        store = InMemorySessionStore()
        code = await store.create(
            connection="conn-1",
            os="linux",
            hostname="h1",
            version="0.1.0",
            ttl_seconds=30,
            capabilities=None,
        )
        record = await store.get(code)
        assert record.capabilities == ()

    def test_session_record_defaults_capabilities_for_positional_construction(self):
        # `capabilities` est ajouté **en dernier** avec une valeur par défaut :
        # les constructions positionnelles existantes restent valides.
        record = SessionRecord("123456789", "conn", "linux", "h1", "0.1.0", 0.0, 100.0)
        assert record.capabilities == ()


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
