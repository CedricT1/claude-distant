package main

import (
	"context"
	"errors"
	"runtime"
	"testing"
)

// Red-first tests for interpreter selection (resolveShell / buildShellArgv
// in executor.go, not yet written). lookPath is injected so this stays a
// pure, offline-testable function instead of depending on the real PATH.

func fakeLookPath(available ...string) func(string) (string, error) {
	set := map[string]bool{}
	for _, a := range available {
		set[a] = true
	}
	return func(name string) (string, error) {
		if set[name] {
			return "/usr/bin/" + name, nil
		}
		return "", errors.New("executable file not found in $PATH")
	}
}

func TestResolveShell_AutoOnWindowsPrefersPwsh(t *testing.T) {
	bin, style, err := resolveShell("windows", "auto", fakeLookPath("pwsh", "powershell"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bin != "pwsh" || style != stylePowerShell {
		t.Errorf("got bin=%q style=%v, want pwsh/stylePowerShell", bin, style)
	}
}

func TestResolveShell_AutoOnWindowsFallsBackToPowerShell(t *testing.T) {
	bin, style, err := resolveShell("windows", "auto", fakeLookPath("powershell"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bin != "powershell" || style != stylePowerShell {
		t.Errorf("got bin=%q style=%v, want powershell/stylePowerShell", bin, style)
	}
}

func TestResolveShell_AutoOnWindowsErrorsWhenNoneFound(t *testing.T) {
	_, _, err := resolveShell("windows", "auto", fakeLookPath())
	if err == nil {
		t.Error("expected error when neither pwsh nor powershell is available")
	}
}

func TestResolveShell_AutoOnLinuxUsesBash(t *testing.T) {
	bin, style, err := resolveShell("linux", "auto", fakeLookPath("bash", "sh"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bin != "bash" || style != stylePosix {
		t.Errorf("got bin=%q style=%v, want bash/stylePosix", bin, style)
	}
}

func TestResolveShell_AutoOnLinuxErrorsWhenBashMissing(t *testing.T) {
	_, _, err := resolveShell("linux", "auto", fakeLookPath("sh"))
	if err == nil {
		t.Error("expected error when bash is unavailable, even though sh is present (auto must not silently substitute)")
	}
}

func TestResolveShell_ExplicitOverrides(t *testing.T) {
	lp := fakeLookPath("bash", "sh", "pwsh", "powershell")

	cases := []struct {
		override  string
		wantBin   string
		wantStyle shellStyle
	}{
		{"bash", "bash", stylePosix},
		{"sh", "sh", stylePosix},
		{"pwsh", "pwsh", stylePowerShell},
		{"powershell", "powershell", stylePowerShell},
		{"PowerShell", "powershell", stylePowerShell}, // case-insensitive
	}
	for _, c := range cases {
		bin, style, err := resolveShell("linux", c.override, lp)
		if err != nil {
			t.Errorf("override %q: unexpected error: %v", c.override, err)
			continue
		}
		if bin != c.wantBin || style != c.wantStyle {
			t.Errorf("override %q: got bin=%q style=%v, want %q/%v", c.override, bin, style, c.wantBin, c.wantStyle)
		}
	}
}

func TestResolveShell_ExplicitOverrideMissingBinaryErrors(t *testing.T) {
	_, _, err := resolveShell("linux", "pwsh", fakeLookPath("bash"))
	if err == nil {
		t.Error("expected error when explicitly requested shell is not installed")
	}
}

func TestResolveShell_UnsupportedOverrideErrors(t *testing.T) {
	_, _, err := resolveShell("linux", "zsh", fakeLookPath("bash", "zsh"))
	if err == nil {
		t.Error("expected error for a shell override outside auto|powershell|pwsh|bash|sh")
	}
}

func TestBuildShellArgv_PowerShellForcesUTF8AndCommandFlag(t *testing.T) {
	args := buildShellArgv(stylePowerShell, "Get-Process")
	joined := ""
	for _, a := range args {
		joined += a + "|"
	}
	if len(args) < 2 || args[len(args)-2] != "-Command" {
		t.Fatalf("expected -Command as second-to-last arg, got %v", args)
	}
	last := args[len(args)-1]
	if !contains(last, "UTF8") || !contains(last, "Get-Process") {
		t.Errorf("script arg = %q, want UTF-8 preamble and original command", last)
	}
}

func TestBuildShellArgv_PosixUsesDashC(t *testing.T) {
	args := buildShellArgv(stylePosix, "df -h")
	if len(args) != 2 || args[0] != "-c" || args[1] != "df -h" {
		t.Errorf("got %v, want [-c \"df -h\"]", args)
	}
}

// Red-first tests for the residue-free workspace wiring: spawned commands
// must default their working directory to the Executor's workDir (the
// client's scratch Workspace, workspace.go) so anything they write without
// an absolute path is removed automatically at shutdown.

func TestBuildPlainCommand_SetsWorkingDirectoryToWorkspace(t *testing.T) {
	e := &Executor{workDir: t.TempDir()}
	cmd, err := e.buildPlainCommand(context.Background(), "true")
	if err != nil {
		t.Fatalf("buildPlainCommand error: %v", err)
	}
	if cmd.Dir != e.workDir {
		t.Errorf("cmd.Dir = %q, want %q", cmd.Dir, e.workDir)
	}
}

func TestBuildPlainCommand_EmptyWorkDirLeavesCmdDirUnset(t *testing.T) {
	e := &Executor{}
	cmd, err := e.buildPlainCommand(context.Background(), "true")
	if err != nil {
		t.Fatalf("buildPlainCommand error: %v", err)
	}
	if cmd.Dir != "" {
		t.Errorf("cmd.Dir = %q, want empty (process's own cwd)", cmd.Dir)
	}
}

func TestBuildShellCommand_SetsWorkingDirectoryToWorkspace(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("assumes bash is available, as on linux CI/dev runners")
	}
	e := &Executor{workDir: t.TempDir()}
	cmd, err := e.buildShellCommand(context.Background(), "echo hi", "bash")
	if err != nil {
		t.Fatalf("buildShellCommand error: %v", err)
	}
	if cmd.Dir != e.workDir {
		t.Errorf("cmd.Dir = %q, want %q", cmd.Dir, e.workDir)
	}
}

// Red-first tests for the "toujours autoriser" guard-rail memory: once the
// operator approves a destructive command with "always", the exact same
// command string must not prompt again for the rest of the session.

func TestExecutor_AlwaysAllowedRemembersExactCommand(t *testing.T) {
	e := NewExecutor(nil, PolicyConfirm, nil, "")
	if e.isAlwaysAllowed("rm -rf /tmp/x") {
		t.Fatal("isAlwaysAllowed = true before any approval, want false")
	}
	e.rememberAlwaysAllowed("rm -rf /tmp/x")
	if !e.isAlwaysAllowed("rm -rf /tmp/x") {
		t.Error("isAlwaysAllowed = false after rememberAlwaysAllowed, want true")
	}
	if e.isAlwaysAllowed("rm -rf /tmp/y") {
		t.Error("isAlwaysAllowed = true for a different command, want false (matching is exact-string only)")
	}
}

func TestExecutor_ResolveApproval_AlwaysAnswerSkipsFuturePrompts(t *testing.T) {
	calls := 0
	confirm := func(command string) (bool, bool) {
		calls++
		return true, true // operator picks "toujours" the one time they're asked
	}
	e := NewExecutor(nil, PolicyConfirm, confirm, "")

	if approved := e.resolveApproval("shutdown -h now"); !approved || calls != 1 {
		t.Fatalf("first resolveApproval: approved=%v calls=%d, want true/1", approved, calls)
	}
	// Second time around, the memorized decision must short-circuit
	// before e.confirm is invoked again.
	if approved := e.resolveApproval("shutdown -h now"); !approved || calls != 1 {
		t.Errorf("second resolveApproval: approved=%v calls=%d, want true/1 (no re-prompt)", approved, calls)
	}
	// A different command was never approved, so it must still prompt.
	if approved := e.resolveApproval("reboot"); !approved || calls != 2 {
		t.Errorf("resolveApproval for a different command: approved=%v calls=%d, want true/2", approved, calls)
	}
}

func TestExecutor_ResolveApproval_OnceAnswerDoesNotMemoize(t *testing.T) {
	calls := 0
	confirm := func(command string) (bool, bool) {
		calls++
		return true, false // operator approves just this once
	}
	e := NewExecutor(nil, PolicyConfirm, confirm, "")

	e.resolveApproval("shutdown -h now")
	e.resolveApproval("shutdown -h now")
	if calls != 2 {
		t.Errorf("calls = %d, want 2 (a plain 'oui' must not be memorized)", calls)
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (func() bool {
		for i := 0; i+len(substr) <= len(s); i++ {
			if s[i:i+len(substr)] == substr {
				return true
			}
		}
		return false
	})()
}
