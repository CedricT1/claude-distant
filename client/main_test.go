package main

import "testing"

// Red-first tests for CLI/env configuration parsing (main.go's parseConfig
// does not exist yet). args/getenv are both injected so this stays a pure,
// unit-testable function instead of touching real os.Args/os.Getenv.

func noEnv(string) string { return "" }

func envMap(m map[string]string) func(string) string {
	return func(key string) string { return m[key] }
}

func TestParseConfig_RequiresURL(t *testing.T) {
	_, err := parseConfig([]string{"-token=abc"}, noEnv)
	if err == nil {
		t.Error("expected error when --url is missing")
	}
}

func TestParseConfig_RequiresToken(t *testing.T) {
	_, err := parseConfig([]string{"-url=wss://relay.example.com/ws/client"}, noEnv)
	if err == nil {
		t.Error("expected error when --token is missing")
	}
}

func TestParseConfig_FlagsTakePrecedence(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://flag.example.com/ws/client", "-token=flagtoken", "-policy=deny"},
		envMap(map[string]string{
			"CLAUDE_DISTANT_URL":    "wss://env.example.com/ws/client",
			"CLAUDE_DISTANT_TOKEN":  "envtoken",
			"CLAUDE_DISTANT_POLICY": "auto",
		}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.url != "wss://flag.example.com/ws/client" {
		t.Errorf("url = %q, want flag value", cfg.url)
	}
	if cfg.token.String() != "flagtoken" {
		t.Errorf("token = %q, want flag value", cfg.token.String())
	}
	if cfg.policy != PolicyDeny {
		t.Errorf("policy = %v, want %v", cfg.policy, PolicyDeny)
	}
}

func TestParseConfig_FallsBackToEnv(t *testing.T) {
	cfg, err := parseConfig(
		[]string{},
		envMap(map[string]string{
			"CLAUDE_DISTANT_URL":   "wss://env.example.com/ws/client",
			"CLAUDE_DISTANT_TOKEN": "envtoken",
		}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.url != "wss://env.example.com/ws/client" || cfg.token.String() != "envtoken" {
		t.Errorf("got url=%q token=%q, want values from env", cfg.url, cfg.token.String())
	}
}

func TestParseConfig_DefaultPolicyIsConfirm(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t"},
		noEnv,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.policy != PolicyConfirm {
		t.Errorf("policy = %v, want %v (default)", cfg.policy, PolicyConfirm)
	}
}

func TestParseConfig_InvalidPolicyErrors(t *testing.T) {
	_, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t", "-policy=bogus"}, noEnv)
	if err == nil {
		t.Error("expected error for invalid --policy value")
	}
}

func TestParseConfig_InsecureFlagDefaultsFalse(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.insecure {
		t.Error("insecure = true, want false by default")
	}
}

func TestParseConfig_InsecureFlagCanBeSet(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t", "-insecure-skip-verify"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.insecure {
		t.Error("insecure = false, want true")
	}
}

func TestParseConfig_SelfDestructDefaultsFalse(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.selfDestruct {
		t.Error("selfDestruct = true, want false by default")
	}
}

func TestParseConfig_SelfDestructFlagEnables(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t", "-self-destruct"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.selfDestruct {
		t.Error("selfDestruct = false, want true when --self-destruct is set")
	}
}

func TestParseConfig_SelfDestructEnvEnables(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t"},
		envMap(map[string]string{"CLAUDE_DISTANT_SELF_DESTRUCT": "true"}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.selfDestruct {
		t.Error("selfDestruct = false, want true when CLAUDE_DISTANT_SELF_DESTRUCT=true")
	}
}

func TestFormatSessionCode_GroupsNineDigits(t *testing.T) {
	got := formatSessionCode("784123678")
	want := "784 123 678"
	if got != want {
		t.Errorf("formatSessionCode = %q, want %q", got, want)
	}
}

func TestFormatSessionCode_LeavesUnexpectedLengthUntouched(t *testing.T) {
	got := formatSessionCode("12345")
	if got != "12345" {
		t.Errorf("formatSessionCode = %q, want unchanged input", got)
	}
}
