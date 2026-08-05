"""Tests unitaires pour relay.mcp_server : les 4 outils MCP, via le vrai SDK.

On instancie un vrai `FastMCP` (relay.mcp_server.create_mcp_server) et on
appelle les outils via l'API publique `mcp.call_tool(name, arguments)`, comme
le ferait un client MCP réel — mais le broker est remplacé par un double de
test (StubBroker) pour isoler la logique d'agrégation des outils du réseau
et du vrai broker (déjà testé dans test_broker.py).
"""
import asyncio
import base64
import hashlib
import json
from contextlib import contextmanager

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from relay.broker import (
    Broker,
    ClientDisconnectedError,
    CommandDeniedError,
    CommandTimeoutError,
    SessionNotFoundError,
)
from relay.mcp_server import create_mcp_server
from relay.session_store import InMemorySessionStore, SessionRecord


class StubBroker:
    """Double de test imitant l'API interne utilisée par mcp_server.py."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.session_info: SessionRecord | None = None
        self.chunks: list[dict] = []
        self.error: Exception | None = None

    async def get_session_info(self, session_code):
        self.calls.append(("get_session_info", session_code))
        return self.session_info

    async def dispatch_command(self, session_code, tool, params, timeout=None):
        self.calls.append(("dispatch_command", session_code, tool, dict(params), timeout))
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk


async def call_tool(mcp, name, arguments):
    """Appelle un outil via l'API publique du SDK et récupère son résultat structuré.

    Les outils sont annotés `-> dict[str, Any]` : le SDK détecte une sortie
    structurée et `call_tool` renvoie `(contenu_texte, dict_structuré)`. On
    retombe sur le parsing JSON du contenu texte si jamais ce n'est pas le cas.
    """
    result = await mcp.call_tool(name, arguments)
    if isinstance(result, tuple):
        _content, structured = result
        return structured
    return json.loads(result[0].text)


def _record(capabilities=("file_transfer",), version="0.2.0", code="123456789"):
    """Construit un `SessionRecord` de test avec les capacités voulues."""
    return SessionRecord(
        code=code,
        connection=object(),
        os="linux",
        hostname="srv01",
        version=version,
        created_at=0.0,
        expires_at=100.0,
        capabilities=capabilities,
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _dispatches(broker) -> list[tuple]:
    """Filtre les appels du StubBroker pour ne garder que les dispatchs réels."""
    return [call for call in broker.calls if call[0] == "dispatch_command"]


@contextmanager
def as_principal(scopes):
    """Simule un appel MCP authentifié porteur de `scopes` (cf. test_mcp_oauth.py)."""
    token = AccessToken(token="t", client_id="harness-1", scopes=list(scopes), expires_at=None, subject="harness-1")
    ctxtoken = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(ctxtoken)


class TestConnectSession:
    async def test_returns_connected_status_for_known_session(self):
        broker = StubBroker()
        broker.session_info = SessionRecord(
            code="123456789",
            connection=object(),
            os="linux",
            hostname="srv01",
            version="1.0",
            created_at=0.0,
            expires_at=100.0,
        )
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "connect_session", {"session_code": "123456789"})
        assert result == {
            "status": "connected",
            "session_code": "123456789",
            "os": "linux",
            "hostname": "srv01",
            "version": "1.0",
            "capabilities": [],
        }

    async def test_returns_not_found_for_unknown_session(self):
        broker = StubBroker()
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "connect_session", {"session_code": "000000000"})
        assert result == {"status": "not_found", "session_code": "000000000"}

    async def test_reports_the_declared_capabilities(self):
        broker = StubBroker()
        broker.session_info = _record(capabilities=("file_transfer",))
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "connect_session", {"session_code": "123456789"})
        assert result["capabilities"] == ["file_transfer"]


class TestRunCommandAggregation:
    async def test_aggregates_stdout_stderr_and_exit_code(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "stream", "stream": "stdout", "data": "line1\n"},
            {"type": "stream", "stream": "stderr", "data": "warn\n"},
            {"type": "stream", "stream": "stdout", "data": "line2\n"},
            {"type": "result", "exit_code": 0, "error": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "123456789", "command": "ls"})
        assert result == {
            "status": "ok",
            "stdout": "line1\nline2\n",
            "stderr": "warn\n",
            "exit_code": 0,
            "error": None,
        }
        assert broker.calls[-1][1:4] == ("123456789", "run_command", {"command": "ls"})

    async def test_passes_timeout_through(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker)
        await call_tool(mcp, "run_command", {"session_code": "1", "command": "ls", "timeout": 12})
        assert broker.calls[-1][4] == 12


class TestRunShell:
    async def test_defaults_shell_to_auto(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker)
        await call_tool(mcp, "run_shell", {"session_code": "1", "command": "echo hi"})
        assert broker.calls[-1][3]["shell"] == "auto"

    async def test_overrides_shell(self):
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None}]
        mcp = create_mcp_server(broker)
        await call_tool(
            mcp, "run_shell", {"session_code": "1", "command": "dir", "shell": "powershell"}
        )
        assert broker.calls[-1][3]["shell"] == "powershell"


class TestErrorHandling:
    async def test_session_not_found_error(self):
        broker = StubBroker()
        broker.error = SessionNotFoundError("nope")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "0", "command": "x"})
        assert result["status"] == "error"
        assert result["error"] == "session_not_found"

    async def test_client_disconnected_error(self):
        broker = StubBroker()
        broker.error = ClientDisconnectedError("bye")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_shell", {"session_code": "0", "command": "x"})
        assert result["error"] == "client_disconnected"

    async def test_timeout_error(self):
        broker = StubBroker()
        broker.error = CommandTimeoutError("too slow")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "0", "command": "x"})
        assert result["error"] == "timeout"

    async def test_command_denied_error(self):
        broker = StubBroker()
        broker.error = CommandDeniedError("commande refusée par la denylist")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "0", "command": "rm -rf /"})
        assert result["status"] == "error"
        assert result["error"] == "denied"


class TestSystemInfo:
    async def test_calls_broker_with_system_info_tool(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "stream", "stream": "stdout", "data": "os=linux\n"},
            {"type": "result", "exit_code": 0, "error": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "system_info", {"session_code": "1"})
        assert result["stdout"] == "os=linux\n"
        assert broker.calls[-1][1:3] == ("1", "system_info")


class TestApprovalResponseAggregation:
    """`approval_response` n'est plus un terminateur : le `result` fixe l'issue.

    Avant correction, une commande **approuvée** sous `policy=confirm` faisait
    retourner un résultat vide (`exit_code: null`) parce que le chunk
    d'approbation écrasait l'issue et coupait le flux.
    """

    async def test_approved_command_reports_the_real_output_and_exit_code(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "approval_response", "approved": True},
            {"type": "stream", "stream": "stdout", "data": "hello\n"},
            {"type": "result", "exit_code": 0, "error": None, "meta": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_shell", {"session_code": "1", "command": "echo hello"})
        assert result["status"] == "ok"
        assert result["stdout"] == "hello\n"
        assert result["exit_code"] == 0
        assert result["error"] is None

    async def test_user_refusal_is_reported_by_the_client_result(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "approval_response", "approved": False},
            {"type": "result", "exit_code": 126, "error": "refused_by_user", "meta": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_shell", {"session_code": "1", "command": "rm -rf /"})
        assert result["exit_code"] == 126
        assert result["error"] == "refused_by_user"

    async def test_policy_refusal_is_reported_by_the_client_result(self):
        broker = StubBroker()
        broker.chunks = [
            {"type": "approval_response", "approved": False},
            {"type": "result", "exit_code": 126, "error": "refused_by_policy", "meta": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "1", "command": "id"})
        assert result["error"] == "refused_by_policy"

    async def test_command_tools_never_expose_a_null_meta(self):
        # Non-régression de sortie : le `meta` (toujours présent dans le chunk,
        # à `None` pour un ancien client) ne doit pas fuiter dans le résultat
        # des outils de commande.
        broker = StubBroker()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "run_command", {"session_code": "1", "command": "ls"})
        assert result == {
            "status": "ok",
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "error": None,
        }


class TestFileTransferConstants:
    """Les noms fixés par la spec sont contractuels (client Go, docs, scopes)."""

    def test_capability_and_scope_names(self):
        from relay.mcp_server import (
            CAPABILITY_FILE_TRANSFER,
            MAX_FILE_TRANSFER_BYTES,
            SCOPE_FILE_READ,
            SCOPE_FILE_WRITE,
            TOOL_SCOPES,
        )

        assert CAPABILITY_FILE_TRANSFER == "file_transfer"
        assert SCOPE_FILE_READ == "file:read"
        assert SCOPE_FILE_WRITE == "file:write"
        assert MAX_FILE_TRANSFER_BYTES == 8 * 1024 * 1024
        assert TOOL_SCOPES["read_file"] == SCOPE_FILE_READ
        assert TOOL_SCOPES["write_file"] == SCOPE_FILE_WRITE


class TestReadFile:
    async def test_reassembles_chunks_and_reports_meta(self):
        content = b"hello world"
        digest = hashlib.sha256(content).hexdigest()
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {"type": "file_chunk", "seq": 0, "data": _b64(b"hello ")},
            {"type": "file_chunk", "seq": 1, "data": _b64(b"world")},
            {
                "type": "result",
                "exit_code": 0,
                "error": None,
                "meta": {"path": "/etc/hosts", "size": 11, "sha256": digest, "truncated": False},
            },
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/etc/hosts"})
        assert result == {
            "status": "ok",
            "path": "/etc/hosts",
            "encoding": "base64",
            "content_base64": _b64(content),
            "size": 11,
            "sha256": digest,
            "truncated": False,
        }
        assert _dispatches(broker)[-1][1:4] == ("1", "read_file", {"path": "/etc/hosts"})

    async def test_chunks_are_reassembled_in_seq_order(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {"type": "file_chunk", "seq": 1, "data": _b64(b"world")},
            {"type": "file_chunk", "seq": 0, "data": _b64(b"hello ")},
            {"type": "result", "exit_code": 0, "error": None, "meta": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/f"})
        assert base64.b64decode(result["content_base64"]) == b"hello world"

    async def test_missing_meta_is_recomputed_from_the_received_bytes(self):
        content = b"abcdef"
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {"type": "file_chunk", "seq": 0, "data": _b64(content)},
            {"type": "result", "exit_code": 0, "error": None, "meta": None},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/f"})
        assert result["status"] == "ok"
        assert result["size"] == len(content)
        assert result["sha256"] == hashlib.sha256(content).hexdigest()
        assert result["truncated"] is False

    async def test_offset_and_max_bytes_are_forwarded_only_when_meaningful(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        mcp = create_mcp_server(broker)
        await call_tool(
            mcp,
            "read_file",
            {"session_code": "1", "path": "/f", "offset": 1024, "max_bytes": 4096},
        )
        assert _dispatches(broker)[-1][3] == {"path": "/f", "offset": 1024, "max_bytes": 4096}

    async def test_truncated_flag_comes_from_meta(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {"type": "file_chunk", "seq": 0, "data": _b64(b"abc")},
            {"type": "result", "exit_code": 0, "error": None, "meta": {"size": 3, "truncated": True}},
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp, "read_file", {"session_code": "1", "path": "/f", "max_bytes": 3}
        )
        assert result["truncated"] is True

    async def test_client_side_error_is_surfaced(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {"type": "result", "exit_code": 1, "error": "file_not_found", "meta": None}
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/nope"})
        assert result["status"] == "error"
        assert result["error"] == "file_not_found"

    async def test_protocol_errors_are_translated(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.error = CommandTimeoutError("too slow")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/f"})
        assert result["status"] == "error"
        assert result["error"] == "timeout"
        assert result["detail"] == "too slow"


class TestReadFileParamValidation:
    """Défaut 3 : `max_bytes=0` était dispatché tel quel (seul `offset` avait
    un garde `if offset:`), et côté client Go `MaxBytes == 0` est
    indiscernable d'un `max_bytes` absent (`p.MaxBytes > 0` retombe sur
    "pas de limite") : demander 0 octet renvoyait le fichier entier. Corrigé
    ici, côté relay, en rejetant `max_bytes <= 0` et `offset < 0` avant tout
    dispatch — le client ne peut de toute façon pas distinguer "absent" de
    "zéro" sur le fil.
    """

    async def test_max_bytes_zero_is_rejected_before_dispatch(self):
        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp, "read_file", {"session_code": "1", "path": "/f", "max_bytes": 0}
        )
        assert result["status"] == "error"
        assert result["error"] == "invalid_params"
        assert "detail" in result
        assert _dispatches(broker) == []

    async def test_negative_max_bytes_is_rejected_before_dispatch(self):
        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp, "read_file", {"session_code": "1", "path": "/f", "max_bytes": -1}
        )
        assert result["status"] == "error"
        assert result["error"] == "invalid_params"
        assert _dispatches(broker) == []

    async def test_negative_offset_is_rejected_before_dispatch(self):
        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp, "read_file", {"session_code": "1", "path": "/f", "offset": -1}
        )
        assert result["status"] == "error"
        assert result["error"] == "invalid_params"
        assert _dispatches(broker) == []

    async def test_positive_max_bytes_and_zero_offset_are_still_accepted(self):
        # Non-régression : le cas nominal (offset par défaut, max_bytes
        # positif) ne doit pas être affecté par la validation.
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp, "read_file", {"session_code": "1", "path": "/f", "max_bytes": 10}
        )
        assert result["status"] == "ok"
        assert _dispatches(broker)[-1][3] == {"path": "/f", "max_bytes": 10}


class CountingChunkBroker(StubBroker):
    """StubBroker qui compte les chunks effectivement tirés par le consommateur.

    Sert à prouver qu'un dépassement du plafond de lecture interrompt le flux
    *avant* d'avoir consommé tous les chunks disponibles, plutôt que de tout
    accumuler puis de vérifier la taille a posteriori.
    """

    def __init__(self) -> None:
        super().__init__()
        self.yielded = 0

    async def dispatch_command(self, session_code, tool, params, timeout=None):
        self.calls.append(("dispatch_command", session_code, tool, dict(params), timeout))
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class _FakeConnection:
    """Connexion factice minimale (send_json only), à l'image de test_broker.py."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class TestReadFileSizeCeiling:
    """Défaut 2 : rien ne plafonnait le flux `file_chunk` d'une lecture, alors
    que le timeout du broker est par tranche (pas global) — un client qui
    enchaîne les tranches sans jamais envoyer de `result` pouvait faire
    grossir la mémoire du relay sans borne.
    """

    async def test_oversized_stream_is_rejected_without_draining_every_chunk(self, monkeypatch):
        # Plafond artificiellement bas pour ne pas avoir à générer des Mio de
        # données dans le test.
        monkeypatch.setattr("relay.mcp_server.MAX_FILE_TRANSFER_BYTES", 32)
        broker = CountingChunkBroker()
        broker.session_info = _record()
        chunk_data = _b64(b"x" * 24)  # 32 caractères base64 par tranche
        # Beaucoup plus de tranches que nécessaire pour dépasser le plafond :
        # si la correction fonctionne, le consommateur s'arrête bien avant la
        # fin de la liste.
        broker.chunks = [
            {"type": "file_chunk", "seq": i, "data": chunk_data} for i in range(500)
        ] + [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        mcp = create_mcp_server(broker)

        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/f"})

        assert result["status"] == "error"
        assert result["error"] == "file_too_large"
        assert broker.yielded < 500

    async def test_broker_pending_does_not_leak_when_cap_is_exceeded(self, monkeypatch):
        # Contrairement au stub ci-dessus, on utilise ici un vrai `Broker`
        # pour vérifier que le générateur `dispatch_command` est bien fermé
        # (`aclose()`) en cas de dépassement, et ne laisse pas d'entrée
        # traîner dans `Broker._pending`.
        monkeypatch.setattr("relay.mcp_server.MAX_FILE_TRANSFER_BYTES", 32)
        store = InMemorySessionStore()
        broker = Broker(session_store=store, default_ttl_seconds=30, command_timeout=5)
        conn = _FakeConnection()
        code = await broker.register_connection(
            conn, os="linux", hostname="h", version="0.2.0", capabilities=["file_transfer"]
        )
        mcp = create_mcp_server(broker)

        async def run():
            return await call_tool(mcp, "read_file", {"session_code": code, "path": "/f"})

        task = asyncio.create_task(run())
        await asyncio.sleep(0)  # laisse la commande partir vers le "client"
        request_id = conn.sent[0]["request_id"]

        chunk_data = _b64(b"x" * 24)  # 32 caractères base64 par tranche
        # Un client compromis qui n'enverrait jamais de `result` : sans
        # plafond, `dispatch_command` resterait ouvert indéfiniment.
        for seq in range(50):
            await broker.handle_client_message(
                conn,
                {"type": "file_chunk", "request_id": request_id, "seq": seq, "data": chunk_data},
            )

        result = await task
        assert result["status"] == "error"
        assert result["error"] == "file_too_large"
        assert broker._pending == {}


class TestWriteFile:
    async def test_nominal_write_reports_bytes_written(self):
        content = b"hello"
        digest = hashlib.sha256(content).hexdigest()
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {
                "type": "result",
                "exit_code": 0,
                "error": None,
                "meta": {"path": "/tmp/f", "bytes_written": 5, "sha256": digest},
            }
        ]
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp,
            "write_file",
            {"session_code": "1", "path": "/tmp/f", "content_base64": _b64(content)},
        )
        assert result == {
            "status": "ok",
            "path": "/tmp/f",
            "bytes_written": 5,
            "sha256": digest,
        }
        assert _dispatches(broker)[-1][1:4] == (
            "1",
            "write_file",
            {"path": "/tmp/f", "content_base64": _b64(content)},
        )

    async def test_non_default_options_are_forwarded(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        mcp = create_mcp_server(broker)
        await call_tool(
            mcp,
            "write_file",
            {
                "session_code": "1",
                "path": "/tmp/f",
                "content_base64": _b64(b"x"),
                "mode": "0600",
                "create_dirs": True,
                "overwrite": False,
            },
        )
        params = _dispatches(broker)[-1][3]
        assert params["mode"] == "0600"
        assert params["create_dirs"] is True
        assert params["overwrite"] is False

    async def test_invalid_base64_is_rejected_before_dispatch(self):
        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp,
            "write_file",
            {"session_code": "1", "path": "/tmp/f", "content_base64": "pas du base64 !!"},
        )
        assert result["status"] == "error"
        assert result["error"] == "invalid_base64"
        assert "detail" in result
        assert _dispatches(broker) == []

    async def test_payload_above_the_ceiling_is_rejected_before_dispatch(self):
        from relay.mcp_server import MAX_FILE_TRANSFER_BYTES

        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker)
        oversized = _b64(b"\0" * (MAX_FILE_TRANSFER_BYTES + 1))
        result = await call_tool(
            mcp,
            "write_file",
            {"session_code": "1", "path": "/tmp/f", "content_base64": oversized},
        )
        assert result["status"] == "error"
        assert result["error"] == "file_too_large"
        assert "detail" in result
        assert _dispatches(broker) == []

    async def test_client_side_error_is_surfaced(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [{"type": "result", "exit_code": 1, "error": "file_exists", "meta": None}]
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp,
            "write_file",
            {"session_code": "1", "path": "/tmp/f", "content_base64": _b64(b"x")},
        )
        assert result["status"] == "error"
        assert result["error"] == "file_exists"


class TestCapabilityGuard:
    """Cœur de la rétrocompatibilité : jamais de `command` inconnue à un ancien client."""

    async def test_read_file_on_a_client_without_the_capability(self):
        broker = StubBroker()
        broker.session_info = _record(capabilities=(), version="0.1.0")
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/etc/hosts"})
        assert result["status"] == "error"
        assert result["error"] == "unsupported_by_client"
        assert result["client_version"] == "0.1.0"
        assert result["capabilities"] == []
        assert "0.2.0" in result["detail"]
        assert _dispatches(broker) == []

    async def test_write_file_on_a_client_without_the_capability(self):
        broker = StubBroker()
        broker.session_info = _record(capabilities=(), version="0.1.0")
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp,
            "write_file",
            {"session_code": "1", "path": "/tmp/f", "content_base64": _b64(b"x")},
        )
        assert result["error"] == "unsupported_by_client"
        assert _dispatches(broker) == []

    async def test_unknown_session_reports_session_not_found(self):
        broker = StubBroker()
        broker.session_info = None
        mcp = create_mcp_server(broker)
        result = await call_tool(mcp, "read_file", {"session_code": "000000000", "path": "/f"})
        assert result["status"] == "error"
        assert result["error"] == "session_not_found"
        assert _dispatches(broker) == []

    async def test_unknown_session_on_write_reports_session_not_found(self):
        broker = StubBroker()
        broker.session_info = None
        mcp = create_mcp_server(broker)
        result = await call_tool(
            mcp,
            "write_file",
            {"session_code": "000000000", "path": "/f", "content_base64": _b64(b"x")},
        )
        assert result["error"] == "session_not_found"
        assert _dispatches(broker) == []


class TestFileToolScopes:
    """Mode oauth : les jetons déjà émis (sans `file:*`) se voient refuser les nouveaux outils."""

    async def test_read_file_denied_without_file_read_scope(self):
        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["command:execute", "session:connect"]):
            result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/f"})
        assert result == {
            "status": "error",
            "error": "forbidden_scope",
            "detail": "le jeton ne porte pas le scope requis : 'file:read'",
        }
        assert broker.calls == []  # ni capacité, ni dispatch : refus immédiat

    async def test_read_file_allowed_with_file_read_scope(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [
            {"type": "file_chunk", "seq": 0, "data": _b64(b"ok")},
            {"type": "result", "exit_code": 0, "error": None, "meta": None},
        ]
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["file:read"]):
            result = await call_tool(mcp, "read_file", {"session_code": "1", "path": "/f"})
        assert result["status"] == "ok"

    async def test_write_file_denied_without_file_write_scope(self):
        broker = StubBroker()
        broker.session_info = _record()
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["file:read"]):
            result = await call_tool(
                mcp,
                "write_file",
                {"session_code": "1", "path": "/f", "content_base64": _b64(b"x")},
            )
        assert result["error"] == "forbidden_scope"
        assert broker.calls == []

    async def test_write_file_allowed_with_file_write_scope(self):
        broker = StubBroker()
        broker.session_info = _record()
        broker.chunks = [{"type": "result", "exit_code": 0, "error": None, "meta": None}]
        mcp = create_mcp_server(broker, require_scopes=True)
        with as_principal(["file:write"]):
            result = await call_tool(
                mcp,
                "write_file",
                {"session_code": "1", "path": "/f", "content_base64": _b64(b"x")},
            )
        assert result["status"] == "ok"
