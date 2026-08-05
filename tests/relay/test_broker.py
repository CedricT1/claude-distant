"""Tests unitaires pour relay.broker : corrélation request_id et erreurs."""
import asyncio

import pytest

from relay.broker import (
    Broker,
    ClientDisconnectedError,
    CommandDeniedError,
    CommandTimeoutError,
    SessionNotFoundError,
)
from relay.command_policy import CommandPolicy
from relay.session_store import InMemorySessionStore


class FakeConnection:
    """Connexion client factice : capture les messages envoyés, sans réseau."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.fixture
def store():
    return InMemorySessionStore()


@pytest.fixture
def broker(store):
    return Broker(session_store=store, default_ttl_seconds=30, command_timeout=1)


class TestDispatchCommandHappyPath:
    async def test_aggregates_stream_then_result(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h1", version="1.0")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_shell", {"command": "echo hi"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)  # laisse dispatch_command envoyer la commande au client
        assert len(conn.sent) == 1
        sent = conn.sent[0]
        assert sent["type"] == "command"
        assert sent["tool"] == "run_shell"
        assert sent["params"] == {"command": "echo hi"}
        request_id = sent["request_id"]

        await broker.handle_client_message(
            conn, {"type": "stream", "request_id": request_id, "stream": "stdout", "data": "hi\n"}
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )

        chunks = await task
        assert chunks == [
            {"type": "stream", "stream": "stdout", "data": "hi\n"},
            {"type": "result", "exit_code": 0, "error": None, "meta": None},
        ]

    async def test_different_requests_do_not_cross_talk(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h1", version="1.0")

        async def run(command):
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": command}):
                chunks.append(chunk)
            return chunks

        task_a = asyncio.create_task(run("a"))
        await asyncio.sleep(0)
        task_b = asyncio.create_task(run("b"))
        await asyncio.sleep(0)

        assert len(conn.sent) == 2
        req_a = conn.sent[0]["request_id"]
        req_b = conn.sent[1]["request_id"]
        assert req_a != req_b

        # Répond à B d'abord, puis à A : chaque tâche ne doit recevoir que ses propres chunks.
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": req_b, "exit_code": 1, "error": None}
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": req_a, "exit_code": 0, "error": None}
        )

        result_a = await task_a
        result_b = await task_b
        assert result_a == [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        assert result_b == [{"type": "result", "exit_code": 1, "error": None, "meta": None}]


class TestDispatchCommandErrors:
    async def test_unknown_session_raises(self, broker):
        with pytest.raises(SessionNotFoundError):
            async for _ in broker.dispatch_command("000000000", "run_command", {"command": "x"}):
                pass

    async def test_expired_session_raises(self, broker, monkeypatch):
        conn = FakeConnection()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        fake_time[0] += 3600
        with pytest.raises(SessionNotFoundError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "x"}):
                pass

    async def test_client_disconnect_mid_command_raises(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        await broker.unregister_connection(conn)

        with pytest.raises(ClientDisconnectedError):
            await task

    async def test_timeout_when_no_response(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        with pytest.raises(CommandTimeoutError):
            async for _ in broker.dispatch_command(
                code, "run_command", {"command": "x"}, timeout=0.05
            ):
                pass


class FakeAuditLog:
    """Double de test pour AuditLog : capture les événements sans toucher au disque."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> dict:
        self.events.append(event)
        return event


class TestCommandPolicyWiring:
    async def test_denied_command_never_reaches_client(self, store):
        conn = FakeConnection()
        policy = CommandPolicy(denylist=["rm -rf"])
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                         command_policy=policy, audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        with pytest.raises(CommandDeniedError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "rm -rf /"}):
                pass

        assert conn.sent == []  # jamais envoyée au client

    async def test_denied_command_is_audited(self, store):
        conn = FakeConnection()
        policy = CommandPolicy(denylist=["rm -rf"])
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                         command_policy=policy, audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        with pytest.raises(CommandDeniedError):
            async for _ in broker.dispatch_command(code, "run_command", {"command": "rm -rf /"}):
                pass

        assert len(audit.events) == 1
        assert audit.events[0]["decision"] == "denied"
        assert audit.events[0]["session_code"] == code
        assert audit.events[0]["tool"] == "run_command"

    async def test_allowed_command_is_audited_with_outcome(self, store):
        conn = FakeConnection()
        policy = CommandPolicy()  # permissive
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                         command_policy=policy, audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            async for _ in broker.dispatch_command(code, "run_command", {"command": "echo hi"}):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        await task

        assert len(audit.events) == 1
        assert audit.events[0]["decision"] == "allowed"
        assert audit.events[0]["outcome"] == {"exit_code": 0, "error": None}

    async def test_without_policy_or_audit_behaves_as_before(self, broker):
        # `broker` (fixture) est construit sans command_policy/audit_log : aucune
        # régression pour les usages existants du MVP.
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        chunks = await task
        assert chunks == [{"type": "result", "exit_code": 0, "error": None, "meta": None}]


class TestCapabilitiesRegistration:
    """`register_connection` transmet (ou non) les capacités déclarées au store."""

    async def test_register_without_capabilities_stores_empty_tuple(self, broker, store):
        # Ancien client : l'appel historique à 4 arguments reste valide.
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="0.1.0")
        record = await store.get(code)
        assert record.capabilities == ()

    async def test_register_with_capabilities_propagates_them(self, broker, store):
        conn = FakeConnection()
        code = await broker.register_connection(
            conn, os="linux", hostname="h", version="0.2.0", capabilities=["file_transfer"]
        )
        record = await store.get(code)
        assert record.capabilities == ("file_transfer",)


class TestFileChunkRouting:
    """`file_chunk` est routé comme un chunk **non final**, à l'image de `stream`."""

    async def test_file_chunks_are_forwarded_then_terminated_by_result(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(
            conn, os="linux", hostname="h", version="0.2.0", capabilities=["file_transfer"]
        )

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "read_file", {"path": "/etc/hosts"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]

        await broker.handle_client_message(
            conn, {"type": "file_chunk", "request_id": request_id, "seq": 0, "data": "aGVsbG8g"}
        )
        await broker.handle_client_message(
            conn, {"type": "file_chunk", "request_id": request_id, "seq": 1, "data": "d29ybGQ="}
        )
        await broker.handle_client_message(
            conn,
            {
                "type": "result",
                "request_id": request_id,
                "exit_code": 0,
                "error": None,
                "meta": {"path": "/etc/hosts", "size": 11, "sha256": "abc", "truncated": False},
            },
        )

        chunks = await task
        assert chunks == [
            {"type": "file_chunk", "seq": 0, "data": "aGVsbG8g"},
            {"type": "file_chunk", "seq": 1, "data": "d29ybGQ="},
            {
                "type": "result",
                "exit_code": 0,
                "error": None,
                "meta": {"path": "/etc/hosts", "size": 11, "sha256": "abc", "truncated": False},
            },
        ]

    async def test_file_chunk_from_another_connection_is_ignored(self, broker):
        conn = FakeConnection()
        intruder = FakeConnection()
        code = await broker.register_connection(
            conn, os="linux", hostname="h", version="0.2.0", capabilities=["file_transfer"]
        )

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "read_file", {"path": "/etc/hosts"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]

        await broker.handle_client_message(
            intruder, {"type": "file_chunk", "request_id": request_id, "seq": 0, "data": "cHduZWQ="}
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )

        chunks = await task
        assert chunks == [{"type": "result", "exit_code": 0, "error": None, "meta": None}]


class TestResultMeta:
    """Le champ optionnel `meta` du `result` est propagé tel quel dans le chunk."""

    async def test_meta_is_propagated_when_present(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="0.2.0")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "write_file", {"path": "/tmp/f"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn,
            {
                "type": "result",
                "request_id": request_id,
                "exit_code": 0,
                "error": None,
                "meta": {"bytes_written": 5, "sha256": "deadbeef"},
            },
        )
        chunks = await task
        assert chunks[-1]["meta"] == {"bytes_written": 5, "sha256": "deadbeef"}

    async def test_meta_is_none_for_a_legacy_client(self, broker):
        # Un ancien client n'émet jamais `meta` : le chunk porte `None`, et les
        # agrégateurs de commande doivent continuer à ignorer ce champ.
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="0.1.0")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_command", {"command": "x"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        chunks = await task
        assert chunks == [{"type": "result", "exit_code": 0, "error": None, "meta": None}]


class TestApprovalResponseIsNotFinal:
    """`approval_response` n'est **pas** un terminateur : seul `result` l'est.

    Correction du bug historique : sous `policy=confirm`, le client Go émet
    `approval_response(approved=true)` *avant* d'exécuter la commande. Le
    traiter comme final jetait les `stream`/`result` qui suivaient.
    """

    async def test_approved_then_stream_and_result_reach_the_harness(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_shell", {"command": "ls"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]

        await broker.handle_client_message(
            conn, {"type": "approval_response", "request_id": request_id, "approved": True}
        )
        await broker.handle_client_message(
            conn,
            {"type": "stream", "request_id": request_id, "stream": "stdout", "data": "a.txt\n"},
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )

        chunks = await task
        assert chunks == [
            {"type": "approval_response", "approved": True},
            {"type": "stream", "stream": "stdout", "data": "a.txt\n"},
            {"type": "result", "exit_code": 0, "error": None, "meta": None},
        ]

    async def test_refusal_is_still_terminated_by_the_client_result(self, broker):
        conn = FakeConnection()
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            chunks = []
            async for chunk in broker.dispatch_command(code, "run_shell", {"command": "rm -rf /"}):
                chunks.append(chunk)
            return chunks

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]

        await broker.handle_client_message(
            conn, {"type": "approval_response", "request_id": request_id, "approved": False}
        )
        await broker.handle_client_message(
            conn,
            {"type": "result", "request_id": request_id, "exit_code": 126, "error": "refused_by_user"},
        )

        chunks = await task
        assert chunks == [
            {"type": "approval_response", "approved": False},
            {"type": "result", "exit_code": 126, "error": "refused_by_user", "meta": None},
        ]

    async def test_audit_outcome_comes_from_the_final_result(self, store):
        conn = FakeConnection()
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                        command_policy=CommandPolicy(), audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            async for _ in broker.dispatch_command(code, "run_shell", {"command": "ls"}):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "approval_response", "request_id": request_id, "approved": True}
        )
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 7, "error": None}
        )
        await task

        assert audit.events[-1]["outcome"] == {"exit_code": 7, "error": None}


class TestAuditParamsRedaction:
    """`content_base64` ne doit jamais atterrir tel quel dans le journal d'audit."""

    async def test_content_base64_is_replaced_by_a_size_marker(self, store):
        conn = FakeConnection()
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                        audit_log=audit)
        code = await broker.register_connection(
            conn, os="linux", hostname="h", version="0.2.0", capabilities=["file_transfer"]
        )
        payload = "QUJDRA==" * 64
        params = {"path": "/tmp/f", "content_base64": payload}

        async def run():
            async for _ in broker.dispatch_command(code, "write_file", params):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        await task

        recorded = audit.events[-1]["params"]
        assert recorded["path"] == "/tmp/f"
        assert recorded["content_base64"] == f"<base64 redacted: {len(payload)} chars>"
        # Le dict d'origine (et donc la trame envoyée au client) reste intact.
        assert params["content_base64"] == payload
        assert conn.sent[0]["params"]["content_base64"] == payload

    async def test_redaction_also_applies_to_a_denied_dispatch(self, store):
        conn = FakeConnection()
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                        command_policy=CommandPolicy(max_commands_per_session=0), audit_log=audit)
        code = await broker.register_connection(
            conn, os="linux", hostname="h", version="0.2.0", capabilities=["file_transfer"]
        )
        payload = "QUJDRA=="

        with pytest.raises(CommandDeniedError):
            async for _ in broker.dispatch_command(
                code, "write_file", {"path": "/tmp/f", "content_base64": payload}
            ):
                pass

        assert audit.events[-1]["decision"] == "denied"
        assert audit.events[-1]["params"]["content_base64"] == (
            f"<base64 redacted: {len(payload)} chars>"
        )

    async def test_params_without_content_are_left_untouched(self, store):
        conn = FakeConnection()
        audit = FakeAuditLog()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=1,
                        audit_log=audit)
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")

        async def run():
            async for _ in broker.dispatch_command(code, "run_shell", {"command": "echo hi"}):
                pass

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        request_id = conn.sent[0]["request_id"]
        await broker.handle_client_message(
            conn, {"type": "result", "request_id": request_id, "exit_code": 0, "error": None}
        )
        await task

        assert audit.events[-1]["params"] == {"command": "echo hi"}


class TestHeartbeat:
    async def test_heartbeat_extends_ttl(self, broker, store, monkeypatch):
        conn = FakeConnection()
        fake_time = [1000.0]
        monkeypatch.setattr("relay.session_store.time.monotonic", lambda: fake_time[0])
        code = await broker.register_connection(conn, os="linux", hostname="h", version="1")
        fake_time[0] += 25  # proche de la fin du TTL par défaut (30s)
        assert await broker.heartbeat(code) is True
        fake_time[0] += 25  # dépasserait le TTL initial, mais le heartbeat l'a prolongé
        assert await store.get(code) is not None

    async def test_heartbeat_unknown_session_returns_false(self, broker):
        assert await broker.heartbeat("000000000") is False
