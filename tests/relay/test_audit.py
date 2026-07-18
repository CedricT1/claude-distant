"""Tests unitaires pour relay.audit : journal d'audit JSONL chaîné par hash."""
import json

import pytest

from relay.audit import AuditLog, verify_chain


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "audit.log"


class TestRecord:
    def test_record_writes_one_jsonl_line(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "123456789", "tool": "run_command", "decision": "allowed"})
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["session_code"] == "123456789"
        assert entry["tool"] == "run_command"
        assert entry["decision"] == "allowed"

    def test_record_adds_timestamp_iso8601_utc(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "1", "tool": "run_command", "decision": "allowed"})
        entry = json.loads(log_path.read_text().splitlines()[0])
        assert "timestamp" in entry
        assert entry["timestamp"].endswith("+00:00") or entry["timestamp"].endswith("Z")

    def test_record_truncates_large_params(self, log_path):
        log = AuditLog(path=log_path)
        huge_command = "x" * 10_000
        log.record(
            {
                "session_code": "1",
                "tool": "run_shell",
                "params": {"command": huge_command},
                "decision": "allowed",
            }
        )
        entry = json.loads(log_path.read_text().splitlines()[0])
        # les données massives ne doivent pas être recopiées telles quelles
        serialized = json.dumps(entry)
        assert len(serialized) < len(huge_command)

    def test_first_entry_has_genesis_prev_hash(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "1", "tool": "t", "decision": "allowed"})
        entry = json.loads(log_path.read_text().splitlines()[0])
        assert entry["prev_hash"] == "0" * 64

    def test_entries_are_chained_by_hash(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "1", "tool": "a", "decision": "allowed"})
        log.record({"session_code": "1", "tool": "b", "decision": "allowed"})
        lines = log_path.read_text().splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert second["prev_hash"] == first["hash"]
        assert first["hash"] != second["hash"]

    def test_reopening_existing_log_continues_chain(self, log_path):
        log1 = AuditLog(path=log_path)
        log1.record({"session_code": "1", "tool": "a", "decision": "allowed"})
        log2 = AuditLog(path=log_path)  # simule un redémarrage du process
        log2.record({"session_code": "1", "tool": "b", "decision": "allowed"})
        lines = log_path.read_text().splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert second["prev_hash"] == first["hash"]


class TestVerifyChain:
    def test_empty_or_missing_log_is_valid(self, log_path):
        assert verify_chain(log_path) is True

    def test_valid_chain_verifies_true(self, log_path):
        log = AuditLog(path=log_path)
        for i in range(5):
            log.record({"session_code": "1", "tool": f"t{i}", "decision": "allowed"})
        assert verify_chain(log_path) is True

    def test_tampering_a_field_breaks_verification(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "1", "tool": "a", "decision": "allowed"})
        log.record({"session_code": "1", "tool": "b", "decision": "allowed"})

        lines = log_path.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["decision"] = "denied"  # falsification après coup
        lines[0] = json.dumps(entry)
        log_path.write_text("\n".join(lines) + "\n")

        assert verify_chain(log_path) is False

    def test_deleting_a_middle_entry_breaks_verification(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "1", "tool": "a", "decision": "allowed"})
        log.record({"session_code": "1", "tool": "b", "decision": "allowed"})
        log.record({"session_code": "1", "tool": "c", "decision": "allowed"})

        lines = log_path.read_text().splitlines()
        del lines[1]  # supprime l'entrée du milieu : casse la chaîne
        log_path.write_text("\n".join(lines) + "\n")

        assert verify_chain(log_path) is False

    def test_reordering_entries_breaks_verification(self, log_path):
        log = AuditLog(path=log_path)
        log.record({"session_code": "1", "tool": "a", "decision": "allowed"})
        log.record({"session_code": "1", "tool": "b", "decision": "allowed"})

        lines = log_path.read_text().splitlines()
        lines.reverse()
        log_path.write_text("\n".join(lines) + "\n")

        assert verify_chain(log_path) is False


class TestDefaultPath:
    def test_defaults_to_audit_log_path_env(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_audit.log"
        monkeypatch.setenv("AUDIT_LOG_PATH", str(custom))
        log = AuditLog()
        log.record({"session_code": "1", "tool": "a", "decision": "allowed"})
        assert custom.exists()
