package main

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// Red-first tests for the opt-in --self-destruct behavior (selfdestruct.go
// does not exist yet): disabled by default, enabled via flag or a truthy
// CLAUDE_DISTANT_SELF_DESTRUCT env var; binary path resolution; and the
// cross-platform removal strategy (direct unlink on Linux, a detached
// helper script on Windows since a running .exe can't delete itself).

func TestSelfDestructEnabled_DefaultDisabled(t *testing.T) {
	if selfDestructEnabled(false, noEnv) {
		t.Error("selfDestructEnabled(false, noEnv) = true, want false (disabled by default)")
	}
}

func TestSelfDestructEnabled_FlagEnables(t *testing.T) {
	if !selfDestructEnabled(true, noEnv) {
		t.Error("selfDestructEnabled(true, noEnv) = false, want true")
	}
}

func TestSelfDestructEnabled_TruthyEnvEnables(t *testing.T) {
	cases := []string{"1", "true", "TRUE", "yes", "on", " yes "}
	for _, v := range cases {
		env := envMap(map[string]string{"CLAUDE_DISTANT_SELF_DESTRUCT": v})
		if !selfDestructEnabled(false, env) {
			t.Errorf("selfDestructEnabled(false, env(%q)) = false, want true", v)
		}
	}
}

func TestSelfDestructEnabled_FalsyOrEmptyEnvDoesNotEnable(t *testing.T) {
	cases := []string{"", "0", "false", "no", "bogus"}
	for _, v := range cases {
		env := envMap(map[string]string{"CLAUDE_DISTANT_SELF_DESTRUCT": v})
		if selfDestructEnabled(false, env) {
			t.Errorf("selfDestructEnabled(false, env(%q)) = true, want false", v)
		}
	}
}

func TestResolveExecutablePath_PropagatesError(t *testing.T) {
	wantErr := errors.New("boom")
	_, err := resolveExecutablePath(func() (string, error) { return "", wantErr })
	if err == nil {
		t.Fatal("expected error to propagate")
	}
}

func TestResolveExecutablePath_ReturnsResolvedPath(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "claude-distant-client")
	if err := os.WriteFile(target, []byte("binary"), 0o755); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	got, err := resolveExecutablePath(func() (string, error) { return target, nil })
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// On platforms/paths with no symlink involved, EvalSymlinks should
	// return the same (or an equivalent, e.g. /private/var vs /var on
	// macOS) path unchanged.
	if filepath.Clean(got) == "" {
		t.Errorf("resolveExecutablePath returned empty path")
	}
	if _, err := os.Stat(got); err != nil {
		t.Errorf("resolved path %q does not exist: %v", got, err)
	}
}

func TestSelfDestruct_LinuxRemovesFileDirectly(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "claude-distant-client")
	if err := os.WriteFile(target, []byte("binary"), 0o755); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	if err := selfDestruct("linux", target, os.Getpid()); err != nil {
		t.Fatalf("selfDestruct error: %v", err)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Errorf("binary %q still exists after selfDestruct, err=%v", target, err)
	}
}

func TestBuildWindowsSelfDeleteScript_ReferencesExeAndWaitsForPid(t *testing.T) {
	script := buildWindowsSelfDeleteScript(`C:\Users\test\AppData\Local\Temp\claude-distant-client.exe`, 4242)

	if !strings.Contains(script, strconv.Itoa(4242)) {
		t.Errorf("script does not reference pid 4242:\n%s", script)
	}
	if !strings.Contains(script, `claude-distant-client.exe`) {
		t.Errorf("script does not reference the target exe:\n%s", script)
	}
	if !strings.Contains(strings.ToLower(script), "del") {
		t.Errorf("script does not contain a delete command:\n%s", script)
	}
	// The helper script must delete itself too, or it becomes the trace
	// it was meant to avoid leaving behind.
	if !strings.Contains(script, `%~f0`) {
		t.Errorf("script does not delete itself (%%~f0):\n%s", script)
	}
}
