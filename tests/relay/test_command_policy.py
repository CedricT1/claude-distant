"""Tests unitaires pour relay.command_policy : allow/denylist et quotas."""
from relay.command_policy import CommandPolicy


class TestNoPolicyConfigured:
    def test_allows_everything_by_default(self):
        policy = CommandPolicy()
        decision = policy.check("123", "run_command", {"command": "rm -rf /"})
        assert decision.allowed is True


class TestDenylist:
    def test_denies_matching_command(self):
        policy = CommandPolicy(denylist=["rm -rf"])
        decision = policy.check("123", "run_command", {"command": "rm -rf /"})
        assert decision.allowed is False
        assert decision.reason

    def test_allows_non_matching_command(self):
        policy = CommandPolicy(denylist=["rm -rf"])
        decision = policy.check("123", "run_command", {"command": "ls -la"})
        assert decision.allowed is True

    def test_denylist_supports_regex(self):
        policy = CommandPolicy(denylist=[r"^shutdown\b"])
        assert policy.check("123", "run_shell", {"command": "shutdown -h now"}).allowed is False
        assert policy.check("123", "run_shell", {"command": "echo shutdown"}).allowed is True


class TestAllowlist:
    def test_restrictive_allowlist_blocks_non_matching(self):
        policy = CommandPolicy(allowlist=["^ls", "^df"])
        assert policy.check("123", "run_command", {"command": "ls -la"}).allowed is True
        assert policy.check("123", "run_command", {"command": "df -h"}).allowed is True
        decision = policy.check("123", "run_command", {"command": "rm -rf /"})
        assert decision.allowed is False
        assert decision.reason

    def test_empty_allowlist_allows_everything(self):
        policy = CommandPolicy(allowlist=[])
        assert policy.check("123", "run_command", {"command": "anything"}).allowed is True


class TestDenylistPriorityOverAllowlist:
    def test_deny_wins_even_if_allowlisted(self):
        policy = CommandPolicy(allowlist=["^rm"], denylist=["^rm -rf"])
        decision = policy.check("123", "run_command", {"command": "rm -rf /"})
        assert decision.allowed is False


class TestQuotas:
    def test_max_commands_per_session_enforced(self):
        policy = CommandPolicy(max_commands_per_session=2)
        code = "123456789"
        assert policy.check(code, "run_command", {"command": "a"}).allowed is True
        assert policy.check(code, "run_command", {"command": "b"}).allowed is True
        decision = policy.check(code, "run_command", {"command": "c"})
        assert decision.allowed is False
        assert decision.reason

    def test_quota_is_per_session(self):
        policy = CommandPolicy(max_commands_per_session=1)
        assert policy.check("session-a", "run_command", {"command": "a"}).allowed is True
        # Une autre session a son propre quota, non partagé.
        assert policy.check("session-b", "run_command", {"command": "a"}).allowed is True
        assert policy.check("session-a", "run_command", {"command": "b"}).allowed is False

    def test_rate_limit_per_minute_enforced(self):
        clock = [0.0]
        policy = CommandPolicy(rate_limit_per_minute=2, clock=lambda: clock[0])
        code = "123456789"
        assert policy.check(code, "run_command", {"command": "a"}).allowed is True
        assert policy.check(code, "run_command", {"command": "b"}).allowed is True
        decision = policy.check(code, "run_command", {"command": "c"})
        assert decision.allowed is False

    def test_rate_limit_window_slides(self):
        clock = [0.0]
        policy = CommandPolicy(rate_limit_per_minute=1, clock=lambda: clock[0])
        code = "123456789"
        assert policy.check(code, "run_command", {"command": "a"}).allowed is True
        assert policy.check(code, "run_command", {"command": "b"}).allowed is False
        clock[0] += 61  # sort de la fenêtre glissante de 60s
        assert policy.check(code, "run_command", {"command": "c"}).allowed is True

    def test_denied_commands_do_not_consume_quota(self):
        policy = CommandPolicy(denylist=["forbidden"], max_commands_per_session=1)
        code = "123456789"
        assert policy.check(code, "run_command", {"command": "forbidden"}).allowed is False
        # Le quota n'a pas été consommé par la commande refusée.
        assert policy.check(code, "run_command", {"command": "ok"}).allowed is True


class TestFromEnv:
    def test_reads_denylist_and_allowlist_from_env(self, monkeypatch):
        monkeypatch.setenv("COMMAND_DENYLIST", "rm -rf;shutdown")
        monkeypatch.setenv("COMMAND_ALLOWLIST", "")
        monkeypatch.setenv("MAX_COMMANDS_PER_SESSION", "5")
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "10")
        policy = CommandPolicy.from_env()
        decision = policy.check("1", "run_command", {"command": "rm -rf /"})
        assert decision.allowed is False

    def test_defaults_are_permissive_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("COMMAND_DENYLIST", raising=False)
        monkeypatch.delenv("COMMAND_ALLOWLIST", raising=False)
        monkeypatch.delenv("MAX_COMMANDS_PER_SESSION", raising=False)
        monkeypatch.delenv("RATE_LIMIT_PER_MINUTE", raising=False)
        policy = CommandPolicy.from_env()
        assert policy.check("1", "run_command", {"command": "anything"}).allowed is True


class TestNonCommandTools:
    def test_tools_without_command_param_bypass_pattern_matching(self):
        policy = CommandPolicy(denylist=["anything"])
        decision = policy.check("123", "system_info", {})
        assert decision.allowed is True
