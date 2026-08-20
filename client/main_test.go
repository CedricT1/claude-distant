package main

import (
	"errors"
	"testing"
)

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

// TestParseConfig_DefaultPolicyIsAuto pins the default guard-rail policy:
// with neither --policy nor CLAUDE_DISTANT_POLICY set, the client runs the
// harness's commands without prompting the operator for each one. `confirm`
// (per-operation prompt) and `deny` stay one flag/env var away.
func TestParseConfig_DefaultPolicyIsAuto(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t"},
		noEnv,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.policy != PolicyAuto {
		t.Errorf("policy = %v, want %v (default)", cfg.policy, PolicyAuto)
	}
}

// TestParseConfig_PolicyConfirmStillSelectable is the flip side of the
// default above: switching the per-operation prompt back on must stay a
// single flag (and, below, a single environment variable) away.
func TestParseConfig_PolicyConfirmStillSelectable(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t", "-policy=confirm"},
		noEnv,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.policy != PolicyConfirm {
		t.Errorf("policy = %v, want %v", cfg.policy, PolicyConfirm)
	}
}

func TestParseConfig_PolicyFromEnv(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t"},
		envMap(map[string]string{"CLAUDE_DISTANT_POLICY": "confirm"}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.policy != PolicyConfirm {
		t.Errorf("policy = %v, want %v (from env)", cfg.policy, PolicyConfirm)
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

func TestParseConfig_RemoveOnExitDefaultsFalse(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.removeOnExit {
		t.Error("removeOnExit = true, want false by default")
	}
}

func TestParseConfig_RemoveOnExitFlagEnables(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t", "-remove-on-exit"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.removeOnExit {
		t.Error("removeOnExit = false, want true when --remove-on-exit is set")
	}
}

func TestParseConfig_RemoveOnExitEnvEnables(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t"},
		envMap(map[string]string{"CLAUDE_DISTANT_REMOVE_ON_EXIT": "true"}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.removeOnExit {
		t.Error("removeOnExit = false, want true when CLAUDE_DISTANT_REMOVE_ON_EXIT=true")
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

// --- Three-level resolution for a link-time customized build (buildconfig.go) ---
//
// parseConfigWithDefaults is parseConfig's testable core: flag > env >
// compiled default, first non-empty value wins.
// parseConfig itself just plugs in this package's
// buildRelayURL/buildClientToken/buildIdentitySecret (buildconfig.go),
// which stay "" unless a release build stamps them via -ldflags. These
// tests are written and confirmed red before parseConfigWithDefaults /
// buildDefaults exist.

func TestParseConfigWithDefaults_UsesCompiledDefaultWhenFlagAndEnvEmpty(t *testing.T) {
	cfg, err := parseConfigWithDefaults([]string{}, noEnv, buildDefaults{
		url:   "wss://compiled.example.com/ws/client",
		token: "compiled-token",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.url != "wss://compiled.example.com/ws/client" {
		t.Errorf("url = %q, want compiled default", cfg.url)
	}
	if cfg.token.String() != "compiled-token" {
		t.Errorf("token = %q, want compiled default", cfg.token.String())
	}
}

func TestParseConfigWithDefaults_EnvBeatsCompiledDefault(t *testing.T) {
	cfg, err := parseConfigWithDefaults([]string{}, envMap(map[string]string{
		"CLAUDE_DISTANT_URL":   "wss://env.example.com/ws/client",
		"CLAUDE_DISTANT_TOKEN": "env-token",
	}), buildDefaults{url: "wss://compiled.example.com/ws/client", token: "compiled-token"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.url != "wss://env.example.com/ws/client" || cfg.token.String() != "env-token" {
		t.Errorf("got url=%q token=%q, want env values to win over compiled defaults", cfg.url, cfg.token.String())
	}
}

func TestParseConfigWithDefaults_FlagBeatsEnvBeatsCompiledDefault(t *testing.T) {
	cfg, err := parseConfigWithDefaults(
		[]string{"-url=wss://flag.example.com/ws/client", "-token=flag-token"},
		envMap(map[string]string{
			"CLAUDE_DISTANT_URL":   "wss://env.example.com/ws/client",
			"CLAUDE_DISTANT_TOKEN": "env-token",
		}),
		buildDefaults{url: "wss://compiled.example.com/ws/client", token: "compiled-token"},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.url != "wss://flag.example.com/ws/client" || cfg.token.String() != "flag-token" {
		t.Errorf("got url=%q token=%q, want flag values to win over both env and compiled default", cfg.url, cfg.token.String())
	}
}

func TestParseConfigWithDefaults_ErrorsOnlyWhenAllThreeSourcesEmpty(t *testing.T) {
	if _, err := parseConfigWithDefaults([]string{}, noEnv, buildDefaults{}); err == nil {
		t.Error("expected error when flag, env and compiled default are all empty")
	}
	if _, err := parseConfigWithDefaults([]string{}, noEnv, buildDefaults{url: "wss://x/ws/client", token: "t"}); err != nil {
		t.Errorf("unexpected error once the compiled default alone supplies url+token: %v", err)
	}
}

// Non-regression #1 (mandated by the task spec): an unmodified
// binary — i.e. one whose buildconfig.go vars were left at their zero value
// — must behave EXACTLY as before the compiled-default feature existed.
func TestParseConfig_GenericBuildStillRequiresURLAndToken(t *testing.T) {
	if buildRelayURL != "" || buildClientToken != "" {
		t.Skip("this test binary's package-level buildconfig.go vars are non-empty (unexpected outside a customized build)")
	}
	if _, err := parseConfig([]string{}, noEnv); err == nil {
		t.Error("expected error: a non-customized build must still require --url/--token")
	}
}

func TestParseConfig_IdentitySecretDefaultsToCompiledValue(t *testing.T) {
	cfg, err := parseConfigWithDefaults(
		[]string{"-url=wss://x/ws/client", "-token=t"}, noEnv,
		buildDefaults{identitySecret: "compiled-secret"},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.identitySecret != "compiled-secret" {
		t.Errorf("identitySecret = %q, want compiled-secret", cfg.identitySecret)
	}
}

func TestParseConfig_EphemeralCodeDefaultsFalse(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.ephemeralCode {
		t.Error("ephemeralCode = true, want false by default")
	}
}

func TestParseConfig_EphemeralCodeFlagEnables(t *testing.T) {
	cfg, err := parseConfig([]string{"-url=wss://x/ws/client", "-token=t", "-ephemeral-code"}, noEnv)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.ephemeralCode {
		t.Error("ephemeralCode = false, want true when --ephemeral-code is set")
	}
}

func TestParseConfig_EphemeralCodeEnvEnables(t *testing.T) {
	cfg, err := parseConfig(
		[]string{"-url=wss://x/ws/client", "-token=t"},
		envMap(map[string]string{"CLAUDE_DISTANT_EPHEMERAL_CODE": "true"}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cfg.ephemeralCode {
		t.Error("ephemeralCode = false, want true when CLAUDE_DISTANT_EPHEMERAL_CODE=true")
	}
}

// --- resolveDesiredCode: the register message's desired_code field ---
//
// machineID is injected (MachineID in production) so this is testable
// without touching the real machine (identity.go).

func TestResolveDesiredCode_EphemeralFlagReturnsEmpty(t *testing.T) {
	cfg := config{ephemeralCode: true, identitySecret: "s"}
	got := resolveDesiredCode(cfg, func() (string, error) { return "machine-1", nil })
	if got != "" {
		t.Errorf("resolveDesiredCode = %q, want empty when ephemeralCode is set", got)
	}
}

func TestResolveDesiredCode_DerivesFromMachineIDAndSecret(t *testing.T) {
	cfg := config{identitySecret: "my-secret"}
	got := resolveDesiredCode(cfg, func() (string, error) { return "machine-1", nil })
	want := DeriveSessionCode("my-secret", "machine-1")
	if got != want {
		t.Errorf("resolveDesiredCode = %q, want %q (matching DeriveSessionCode)", got, want)
	}
	if len(got) != 9 {
		t.Errorf("resolveDesiredCode = %q, want a 9-digit code", got)
	}
}

func TestResolveDesiredCode_UsesDefaultSaltWhenNoIdentitySecret(t *testing.T) {
	cfg := config{identitySecret: ""}
	got := resolveDesiredCode(cfg, func() (string, error) { return "machine-1", nil })
	want := DeriveSessionCode(defaultIdentitySalt, "machine-1")
	if got != want {
		t.Errorf("resolveDesiredCode = %q, want %q (derived using defaultIdentitySalt)", got, want)
	}
}

func TestResolveDesiredCode_MachineIDFailureReturnsEmpty(t *testing.T) {
	cfg := config{identitySecret: "s"}
	got := resolveDesiredCode(cfg, func() (string, error) { return "", errors.New("no machine id") })
	if got != "" {
		t.Errorf("resolveDesiredCode = %q, want empty when MachineID fails (never a fatal error)", got)
	}
}
