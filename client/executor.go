package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"
)

const (
	defaultCommandTimeout = 5 * time.Minute
	maxCommandTimeout     = 30 * time.Minute
	streamChunkSize       = 4096
)

// shellStyle indicates how a command string must be embedded in argv for a
// given interpreter family.
type shellStyle int

const (
	stylePowerShell shellStyle = iota
	stylePosix
)

// resolveShell decides which interpreter binary to invoke for run_shell,
// given the requested override and the target OS, following the "auto"
// mapping from docs/PROTOCOL.md: auto -> PowerShell on Windows, Bash on
// Linux. lookPath is injected (exec.LookPath in production) so this stays a
// pure, unit-testable function.
func resolveShell(goos, override string, lookPath func(string) (string, error)) (bin string, style shellStyle, err error) {
	ov := strings.ToLower(strings.TrimSpace(override))
	if ov == "" {
		ov = "auto"
	}

	switch ov {
	case "auto":
		if goos == "windows" {
			if _, e := lookPath("pwsh"); e == nil {
				return "pwsh", stylePowerShell, nil
			}
			if _, e := lookPath("powershell"); e == nil {
				return "powershell", stylePowerShell, nil
			}
			return "", 0, fmt.Errorf("aucun interpréteur PowerShell trouvé (pwsh/powershell)")
		}
		if _, e := lookPath("bash"); e == nil {
			return "bash", stylePosix, nil
		}
		return "", 0, fmt.Errorf("bash introuvable pour shell=auto")
	case "powershell":
		if _, e := lookPath("powershell"); e != nil {
			return "", 0, fmt.Errorf("powershell introuvable: %w", e)
		}
		return "powershell", stylePowerShell, nil
	case "pwsh":
		if _, e := lookPath("pwsh"); e != nil {
			return "", 0, fmt.Errorf("pwsh introuvable: %w", e)
		}
		return "pwsh", stylePowerShell, nil
	case "bash":
		if _, e := lookPath("bash"); e != nil {
			return "", 0, fmt.Errorf("bash introuvable: %w", e)
		}
		return "bash", stylePosix, nil
	case "sh":
		if _, e := lookPath("sh"); e != nil {
			return "", 0, fmt.Errorf("sh introuvable: %w", e)
		}
		return "sh", stylePosix, nil
	default:
		return "", 0, fmt.Errorf("shell non supporté: %q (attendu: auto|powershell|pwsh|bash|sh)", override)
	}
}

// buildShellArgv builds the argv (excluding the interpreter binary itself)
// used to run command under the given shell style. PowerShell is forced
// into UTF-8 for both input and output so streamed data decodes correctly
// regardless of the host's active console code page.
func buildShellArgv(style shellStyle, command string) []string {
	if style == stylePowerShell {
		script := "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
		return []string{"-NoProfile", "-NonInteractive", "-Command", script}
	}
	return []string{"-c", command}
}

// splitArgv performs a reasonable argv split of command for run_command,
// which never spawns a shell. It honors single/double quotes and backslash
// escapes without invoking a real shell interpreter.
func splitArgv(command string) ([]string, error) {
	var args []string
	var cur strings.Builder
	hasCur := false
	inSingle, inDouble := false, false

	flush := func() {
		if hasCur {
			args = append(args, cur.String())
			cur.Reset()
			hasCur = false
		}
	}

	runes := []rune(command)
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		switch {
		case inSingle:
			if r == '\'' {
				inSingle = false
			} else {
				cur.WriteRune(r)
				hasCur = true
			}
		case inDouble:
			if r == '"' {
				inDouble = false
			} else if r == '\\' && i+1 < len(runes) && (runes[i+1] == '"' || runes[i+1] == '\\') {
				i++
				cur.WriteRune(runes[i])
				hasCur = true
			} else {
				cur.WriteRune(r)
				hasCur = true
			}
		case r == '\'':
			inSingle = true
			hasCur = true
		case r == '"':
			inDouble = true
			hasCur = true
		case r == '\\' && i+1 < len(runes):
			i++
			cur.WriteRune(runes[i])
			hasCur = true
		case r == ' ' || r == '\t':
			flush()
		default:
			cur.WriteRune(r)
			hasCur = true
		}
	}
	if inSingle || inDouble {
		return nil, fmt.Errorf("guillemets non fermés dans la commande")
	}
	flush()
	return args, nil
}

// Executor runs commands requested by the harness (via the relay) and
// reports their outcome back over conn, applying the local guard-rail
// policy to potentially destructive commands before running them.
type Executor struct {
	conn    *Conn
	policy  Policy
	confirm func(command string) (approved bool, always bool)
	// workDir is the client's dedicated scratch Workspace directory
	// (workspace.go). Spawned commands default their working directory
	// here so that anything they write without an absolute path lands
	// inside the workspace and is removed automatically at Cleanup() —
	// part of the residue-free guarantee (docs/PLAN.md Phase 6). Empty
	// means "use the process's own current directory" (exec.Cmd's normal
	// default), which keeps this backward compatible for callers that
	// don't have a workspace (e.g. existing tests).
	workDir string

	// alwaysMu guards alwaysAllowed, the set of exact command strings the
	// operator approved with "toujours" at the confirm prompt. It is
	// session-only (never persisted) and reset on client restart.
	alwaysMu      sync.Mutex
	alwaysAllowed map[string]bool
}

// NewExecutor builds an Executor. confirm is invoked only when policy is
// PolicyConfirm and the command is classified destructive; it should block
// until the local operator answers. workDir is the working directory
// spawned commands run in (see the Executor.workDir field doc); pass "" to
// fall back to the process's current directory.
func NewExecutor(conn *Conn, policy Policy, confirm func(command string) (approved bool, always bool), workDir string) *Executor {
	return &Executor{conn: conn, policy: policy, confirm: confirm, workDir: workDir, alwaysAllowed: make(map[string]bool)}
}

// Handle dispatches a single `command` message to the right tool. It always
// sends a final `result` message, even when the tool name is unknown or
// params fail to decode.
func (e *Executor) Handle(ctx context.Context, cmd CommandMessage) {
	switch cmd.Tool {
	case "run_shell":
		e.runShellOrCommand(ctx, cmd, true)
	case "run_command":
		e.runShellOrCommand(ctx, cmd, false)
	case "system_info":
		e.handleSystemInfo(cmd)
	default:
		e.sendResult(cmd.RequestID, 1, fmt.Sprintf("outil inconnu: %s", cmd.Tool))
	}
}

func (e *Executor) runShellOrCommand(ctx context.Context, cmd CommandMessage, useShell bool) {
	var p RunParams
	if len(cmd.Params) > 0 {
		if err := json.Unmarshal(cmd.Params, &p); err != nil {
			e.sendResult(cmd.RequestID, 1, fmt.Sprintf("params invalides: %v", err))
			return
		}
	}
	if strings.TrimSpace(p.Command) == "" {
		e.sendResult(cmd.RequestID, 1, "params.command manquant")
		return
	}

	if IsDestructive(p.Command) {
		switch e.policy {
		case PolicyAuto:
			// no gate to apply
		case PolicyDeny:
			e.sendApprovalResponse(cmd.RequestID, false)
			e.sendResult(cmd.RequestID, 126, "refused_by_policy")
			return
		case PolicyConfirm:
			approved := e.resolveApproval(p.Command)
			e.sendApprovalResponse(cmd.RequestID, approved)
			if !approved {
				e.sendResult(cmd.RequestID, 126, "refused_by_user")
				return
			}
		}
	}

	timeout := defaultCommandTimeout
	if p.Timeout > 0 {
		timeout = time.Duration(p.Timeout) * time.Second
		if timeout > maxCommandTimeout {
			timeout = maxCommandTimeout
		}
	}

	runCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var execCmd *exec.Cmd
	var err error
	if useShell {
		execCmd, err = e.buildShellCommand(runCtx, p.Command, p.Shell)
	} else {
		execCmd, err = e.buildPlainCommand(runCtx, p.Command)
	}
	if err != nil {
		e.sendResult(cmd.RequestID, 1, err.Error())
		return
	}

	exitCode, runErr := e.runAndStream(runCtx, cmd.RequestID, execCmd)
	if runCtx.Err() == context.DeadlineExceeded {
		e.sendResult(cmd.RequestID, exitCode, "timeout")
		return
	}
	if runErr != nil {
		e.sendResult(cmd.RequestID, exitCode, runErr.Error())
		return
	}
	e.sendResult(cmd.RequestID, exitCode, "")
}

func (e *Executor) buildShellCommand(ctx context.Context, command, shellOverride string) (*exec.Cmd, error) {
	bin, style, err := resolveShell(runtime.GOOS, shellOverride, exec.LookPath)
	if err != nil {
		return nil, err
	}
	args := buildShellArgv(style, command)
	c := exec.CommandContext(ctx, bin, args...)
	if style == stylePosix {
		// C.UTF-8 is present on essentially every modern Linux system
		// (including minimal/server images) without requiring locale
		// generation, unlike en_US.UTF-8.
		c.Env = append(os.Environ(), "LANG=C.UTF-8", "LC_ALL=C.UTF-8")
	}
	c.Dir = e.workDir
	return c, nil
}

func (e *Executor) buildPlainCommand(ctx context.Context, command string) (*exec.Cmd, error) {
	args, err := splitArgv(command)
	if err != nil {
		return nil, err
	}
	if len(args) == 0 {
		return nil, fmt.Errorf("commande vide")
	}
	if _, err := exec.LookPath(args[0]); err != nil {
		return nil, fmt.Errorf("exécutable introuvable: %s", args[0])
	}
	c := exec.CommandContext(ctx, args[0], args[1:]...)
	c.Dir = e.workDir
	return c, nil
}

// runAndStream starts cmd, streams its stdout/stderr as `stream` messages,
// waits for completion (or ctx cancellation, which kills the whole process
// tree), and returns the exit code.
func (e *Executor) runAndStream(ctx context.Context, requestID string, cmd *exec.Cmd) (int, error) {
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return 1, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return 1, err
	}

	if err := startProcess(cmd); err != nil {
		return 1, err
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go e.pump(&wg, requestID, StreamStdout, stdout)
	go e.pump(&wg, requestID, StreamStderr, stderr)

	waitErr := make(chan error, 1)
	go func() { waitErr <- cmd.Wait() }()

	select {
	case <-ctx.Done():
		killProcessTree(cmd)
		<-waitErr
		wg.Wait()
		return 124, ctx.Err()
	case err := <-waitErr:
		wg.Wait()
		if err != nil {
			var exitErr *exec.ExitError
			if errors.As(err, &exitErr) {
				return exitErr.ExitCode(), nil
			}
			return 1, err
		}
		return 0, nil
	}
}

func (e *Executor) pump(wg *sync.WaitGroup, requestID string, kind StreamKind, r io.Reader) {
	defer wg.Done()
	buf := make([]byte, streamChunkSize)
	for {
		n, err := r.Read(buf)
		if n > 0 {
			if sendErr := e.conn.WriteJSON(NewStreamMessage(requestID, kind, string(buf[:n]))); sendErr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

func (e *Executor) handleSystemInfo(cmd CommandMessage) {
	info, err := GetSystemInfo()
	if err != nil {
		e.sendResult(cmd.RequestID, 1, err.Error())
		return
	}
	payload, err := json.Marshal(info)
	if err != nil {
		e.sendResult(cmd.RequestID, 1, err.Error())
		return
	}
	_ = e.conn.WriteJSON(NewStreamMessage(cmd.RequestID, StreamStdout, string(payload)+"\n"))
	e.sendResult(cmd.RequestID, 0, "")
}

func (e *Executor) sendResult(requestID string, exitCode int, errMsg string) {
	_ = e.conn.WriteJSON(NewResultMessage(requestID, exitCode, errMsg))
}

func (e *Executor) sendApprovalResponse(requestID string, approved bool) {
	_ = e.conn.WriteJSON(NewApprovalResponseMessage(requestID, approved))
}

// resolveApproval decides whether a destructive command may run under
// PolicyConfirm. A command already approved with "toujours" earlier in the
// session is approved without re-prompting; otherwise it blocks on
// e.confirm and remembers the decision when the operator answers
// "toujours" (approved && always).
func (e *Executor) resolveApproval(command string) bool {
	if e.isAlwaysAllowed(command) {
		return true
	}
	if e.confirm == nil {
		return false
	}
	approved, always := e.confirm(command)
	if approved && always {
		e.rememberAlwaysAllowed(command)
	}
	return approved
}

func (e *Executor) isAlwaysAllowed(command string) bool {
	e.alwaysMu.Lock()
	defer e.alwaysMu.Unlock()
	return e.alwaysAllowed[command]
}

func (e *Executor) rememberAlwaysAllowed(command string) {
	e.alwaysMu.Lock()
	defer e.alwaysMu.Unlock()
	e.alwaysAllowed[command] = true
}
