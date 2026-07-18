package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

// selfDestructEnabled resolves the --self-destruct flag / the
// CLAUDE_DISTANT_SELF_DESTRUCT env var into a single boolean decision.
// Disabled by default: the flag (or a truthy env value) must explicitly
// opt in, since deleting the running binary is irreversible.
func selfDestructEnabled(flagSet bool, getenv func(string) string) bool {
	if flagSet {
		return true
	}
	return isTruthy(getenv("CLAUDE_DISTANT_SELF_DESTRUCT"))
}

func isTruthy(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

// resolveExecutablePath returns the canonical path to the running binary.
// executable is injected (os.Executable in production) so this stays
// unit-testable without depending on the real running process. Symlinks
// are resolved on a best-effort basis; if that fails, the unresolved path
// from executable() is still returned rather than erroring out, since a
// self-destruct attempt with a slightly-off path is still worth trying.
func resolveExecutablePath(executable func() (string, error)) (string, error) {
	p, err := executable()
	if err != nil {
		return "", fmt.Errorf("résolution du chemin de l'exécutable: %w", err)
	}
	if resolved, evalErr := filepath.EvalSymlinks(p); evalErr == nil {
		return resolved, nil
	}
	return p, nil
}

// selfDestruct best-effort deletes the running binary at exePath.
//
// On Linux/macOS this is a direct unlink: removing a file's directory
// entry while a process still has it open (as the running client does)
// succeeds immediately and is exactly what "self-destruct" needs — no
// helper process required.
//
// On Windows, an executable that is currently running cannot delete its
// own file (the OS keeps it locked). The documented workaround is to spawn
// a small detached helper (.cmd script) that waits for our PID to exit and
// only then deletes the exe — and finally deletes itself, so it doesn't
// become the trace it was meant to avoid leaving behind.
func selfDestruct(goos, exePath string, pid int) error {
	if goos == "windows" {
		return selfDestructWindows(exePath, pid)
	}
	return os.Remove(exePath)
}

// buildWindowsSelfDeleteScript returns the contents of a .cmd script that
// polls for pid to disappear from `tasklist`, deletes exePath, then
// deletes itself (`%~f0` is the script's own full path in a Windows batch
// file).
func buildWindowsSelfDeleteScript(exePath string, pid int) string {
	pidStr := strconv.Itoa(pid)
	var b strings.Builder
	b.WriteString("@echo off\r\n")
	b.WriteString(":wait\r\n")
	b.WriteString("tasklist /FI \"PID eq " + pidStr + "\" 2>NUL | find \"" + pidStr + "\" >NUL\r\n")
	b.WriteString("if not errorlevel 1 (\r\n")
	b.WriteString("  ping -n 2 127.0.0.1 >NUL\r\n")
	b.WriteString("  goto wait\r\n")
	b.WriteString(")\r\n")
	b.WriteString("del /f /q \"" + exePath + "\" >NUL 2>&1\r\n")
	b.WriteString("del /f /q \"%~f0\"\r\n")
	return b.String()
}

// selfDestructWindows writes the helper script to a temp file and launches
// it fully detached (not waited on) so it can outlive this process, then
// returns immediately; the actual deletion happens after this process
// exits.
func selfDestructWindows(exePath string, pid int) error {
	script := buildWindowsSelfDeleteScript(exePath, pid)
	scriptPath := filepath.Join(os.TempDir(), fmt.Sprintf("claude-distant-cleanup-%d.cmd", pid))
	if err := os.WriteFile(scriptPath, []byte(script), 0o700); err != nil {
		return fmt.Errorf("écriture du script d'auto-suppression: %w", err)
	}
	// "start /min" launches the script as its own detached window/process
	// so it keeps running after this process (and this cmd /C) exits.
	cmd := exec.Command("cmd", "/C", "start", "/min", "", scriptPath)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("lancement du script d'auto-suppression: %w", err)
	}
	return nil
}
