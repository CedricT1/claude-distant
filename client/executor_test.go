package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
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
	e := NewExecutor(nil, NewPolicyController(PolicyConfirm), nil, "", nil)
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
	e := NewExecutor(nil, NewPolicyController(PolicyConfirm), confirm, "", nil)

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
	e := NewExecutor(nil, NewPolicyController(PolicyConfirm), confirm, "", nil)

	e.resolveApproval("shutdown -h now")
	e.resolveApproval("shutdown -h now")
	if calls != 2 {
		t.Errorf("calls = %d, want 2 (a plain 'oui' must not be memorized)", calls)
	}
}

// --- Transfert de fichiers : garde-fou local et câblage des messages ---
//
// Conn is a concrete type wrapping a real *websocket.Conn, so these tests
// dial a throwaway relay that decodes everything the Executor writes. That
// exercises the true send path (file_chunk, approval_response, result+meta)
// instead of a hand-rolled double.

// newLoopbackConn returns a live Conn plus the stream of messages the client
// writes to it, both torn down at the end of the test.
func newLoopbackConn(t *testing.T) (*Conn, <-chan map[string]any) {
	t.Helper()

	recv := make(chan map[string]any, 256)
	upgrader := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		for {
			var msg map[string]any
			if err := c.ReadJSON(&msg); err != nil {
				return
			}
			select {
			case recv <- msg:
			default:
			}
		}
	}))

	conn, err := DialRelay(context.Background(), "ws"+strings.TrimPrefix(srv.URL, "http")+"/ws/client", "test-token", false)
	if err != nil {
		srv.Close()
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() {
		_ = conn.Close()
		srv.Close()
	})
	return conn, recv
}

// collectUntilResult drains recv until the terminating `result` message
// arrives, returning everything received in order.
func collectUntilResult(t *testing.T, recv <-chan map[string]any) []map[string]any {
	t.Helper()
	var msgs []map[string]any
	deadline := time.After(5 * time.Second)
	for {
		select {
		case m := <-recv:
			msgs = append(msgs, m)
			if m["type"] == "result" {
				return msgs
			}
		case <-deadline:
			t.Fatalf("timeout: aucun message result reçu (messages = %v)", msgs)
			return nil
		}
	}
}

func messagesOfType(msgs []map[string]any, msgType string) []map[string]any {
	var out []map[string]any
	for _, m := range msgs {
		if m["type"] == msgType {
			out = append(out, m)
		}
	}
	return out
}

// singleMessage returns the only message of msgType, failing otherwise.
func singleMessage(t *testing.T, msgs []map[string]any, msgType string) map[string]any {
	t.Helper()
	got := messagesOfType(msgs, msgType)
	if len(got) != 1 {
		t.Fatalf("%d messages de type %q, attendu 1 (messages = %v)", len(got), msgType, msgs)
	}
	return got[0]
}

func assertResult(t *testing.T, msgs []map[string]any, wantExitCode float64, wantError any) map[string]any {
	t.Helper()
	res := singleMessage(t, msgs, "result")
	if res["exit_code"] != wantExitCode {
		t.Errorf("exit_code = %v, want %v", res["exit_code"], wantExitCode)
	}
	if res["error"] != wantError {
		t.Errorf("error = %v, want %v", res["error"], wantError)
	}
	return res
}

func assertApproval(t *testing.T, msgs []map[string]any, wantApproved bool) {
	t.Helper()
	ar := singleMessage(t, msgs, "approval_response")
	if ar["approved"] != wantApproved {
		t.Errorf("approval_response.approved = %v, want %v", ar["approved"], wantApproved)
	}
}

// fileCommand builds a `command` message for one of the two transfer tools.
func fileCommand(t *testing.T, requestID, tool string, params map[string]any) CommandMessage {
	t.Helper()
	raw, err := json.Marshal(params)
	if err != nil {
		t.Fatalf("marshal params: %v", err)
	}
	return CommandMessage{Type: TypeCommand, RequestID: requestID, Tool: tool, Params: raw}
}

// recordingConfirm captures the descriptions the guard-rail prompt is asked
// about, and answers with the canned decision.
type recordingConfirm struct {
	descriptions []string
	approve      bool
	always       bool
}

func (r *recordingConfirm) fn(description string) (bool, bool) {
	r.descriptions = append(r.descriptions, description)
	return r.approve, r.always
}

func TestExecutor_ReadFile_PolicyDenyRefusesWithoutReadingTheFile(t *testing.T) {
	path := writeTempFile(t, t.TempDir(), "secret.txt", []byte("données confidentielles"))
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: true}
	e := NewExecutor(conn, NewPolicyController(PolicyDeny), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))

	msgs := collectUntilResult(t, recv)
	assertApproval(t, msgs, false)
	assertResult(t, msgs, 126, "refused_by_policy")
	if n := len(messagesOfType(msgs, "file_chunk")); n != 0 {
		t.Errorf("%d file_chunk émis sous PolicyDeny, attendu 0 (aucune exfiltration)", n)
	}
	if len(confirm.descriptions) != 0 {
		t.Errorf("confirm appelé sous PolicyDeny: %v", confirm.descriptions)
	}
}

func TestExecutor_WriteFile_PolicyDenyRefusesWithoutTouchingDisk(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nouveau.txt")
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: true}
	e := NewExecutor(conn, NewPolicyController(PolicyDeny), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "write_file", map[string]any{
		"path":           path,
		"content_base64": base64.StdEncoding.EncodeToString([]byte("charge utile")),
	}))

	msgs := collectUntilResult(t, recv)
	assertApproval(t, msgs, false)
	assertResult(t, msgs, 126, "refused_by_policy")
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Error("le fichier a été écrit alors que la politique est deny")
	}
	if len(confirm.descriptions) != 0 {
		t.Errorf("confirm appelé sous PolicyDeny: %v", confirm.descriptions)
	}
}

func TestExecutor_ReadFile_PolicyConfirmPromptsAndHonorsRefusal(t *testing.T) {
	path := writeTempFile(t, t.TempDir(), "secret.txt", []byte("données confidentielles"))
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: false}
	e := NewExecutor(conn, NewPolicyController(PolicyConfirm), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))

	msgs := collectUntilResult(t, recv)
	wantDesc := "read_file " + path
	if len(confirm.descriptions) != 1 || confirm.descriptions[0] != wantDesc {
		t.Fatalf("descriptions confirmées = %v, want [%q]", confirm.descriptions, wantDesc)
	}
	assertApproval(t, msgs, false)
	assertResult(t, msgs, 126, "refused_by_user")
	if n := len(messagesOfType(msgs, "file_chunk")); n != 0 {
		t.Errorf("%d file_chunk émis après refus, attendu 0", n)
	}
}

func TestExecutor_ReadFile_PolicyConfirmApprovedPerformsTransfer(t *testing.T) {
	content := []byte("ligne 1\nligne 2\n")
	path := writeTempFile(t, t.TempDir(), "data.txt", content)
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: true}
	e := NewExecutor(conn, NewPolicyController(PolicyConfirm), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))

	msgs := collectUntilResult(t, recv)
	if len(confirm.descriptions) != 1 {
		t.Fatalf("confirm appelé %d fois, attendu 1", len(confirm.descriptions))
	}
	assertApproval(t, msgs, true)

	chunks := messagesOfType(msgs, "file_chunk")
	if len(chunks) != 1 {
		t.Fatalf("%d file_chunk reçus, attendu 1", len(chunks))
	}
	if chunks[0]["request_id"] != "r1" || chunks[0]["seq"] != float64(0) {
		t.Errorf("file_chunk inattendu: %v", chunks[0])
	}
	raw, err := base64.StdEncoding.DecodeString(chunks[0]["data"].(string))
	if err != nil {
		t.Fatalf("base64 du chunk: %v", err)
	}
	if string(raw) != string(content) {
		t.Errorf("contenu transféré = %q, want %q", raw, content)
	}

	res := assertResult(t, msgs, 0, nil)
	meta, ok := res["meta"].(map[string]any)
	if !ok {
		t.Fatalf("result.meta = %#v, objet attendu", res["meta"])
	}
	if meta["path"] != path || meta["size"] != float64(len(content)) ||
		meta["sha256"] != sha256Hex(content) || meta["truncated"] != false {
		t.Errorf("meta = %v, attendu path/size/sha256/truncated cohérents", meta)
	}
}

func TestExecutor_WriteFile_PolicyConfirmPromptsWithSizeAndHonorsRefusal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nouveau.txt")
	content := []byte("charge utile")
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: false}
	e := NewExecutor(conn, NewPolicyController(PolicyConfirm), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "write_file", map[string]any{
		"path":           path,
		"content_base64": base64.StdEncoding.EncodeToString(content),
	}))

	msgs := collectUntilResult(t, recv)
	wantDesc := "write_file " + path + " (12 octets)"
	if len(confirm.descriptions) != 1 || confirm.descriptions[0] != wantDesc {
		t.Fatalf("descriptions confirmées = %v, want [%q]", confirm.descriptions, wantDesc)
	}
	assertApproval(t, msgs, false)
	assertResult(t, msgs, 126, "refused_by_user")
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Error("le fichier a été écrit alors que l'opérateur a refusé")
	}
}

func TestExecutor_WriteFile_PolicyConfirmApprovedPerformsTransfer(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nouveau.txt")
	content := []byte("charge utile")
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: true}
	e := NewExecutor(conn, NewPolicyController(PolicyConfirm), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "write_file", map[string]any{
		"path":           path,
		"content_base64": base64.StdEncoding.EncodeToString(content),
	}))

	msgs := collectUntilResult(t, recv)
	assertApproval(t, msgs, true)
	res := assertResult(t, msgs, 0, nil)

	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("relecture: %v", err)
	}
	if string(got) != string(content) {
		t.Errorf("contenu écrit = %q, want %q", got, content)
	}
	meta, ok := res["meta"].(map[string]any)
	if !ok {
		t.Fatalf("result.meta = %#v, objet attendu", res["meta"])
	}
	if meta["path"] != path || meta["bytes_written"] != float64(len(content)) || meta["sha256"] != sha256Hex(content) {
		t.Errorf("meta = %v, attendu path/bytes_written/sha256 cohérents", meta)
	}
}

func TestExecutor_ReadFile_PolicyAutoNeverPrompts(t *testing.T) {
	content := []byte("contenu\n")
	path := writeTempFile(t, t.TempDir(), "data.txt", content)
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: false}
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))

	msgs := collectUntilResult(t, recv)
	if len(confirm.descriptions) != 0 {
		t.Errorf("confirm appelé sous PolicyAuto: %v", confirm.descriptions)
	}
	if n := len(messagesOfType(msgs, "approval_response")); n != 0 {
		t.Errorf("%d approval_response sous PolicyAuto, attendu 0 (aucun garde-fou engagé)", n)
	}
	if n := len(messagesOfType(msgs, "file_chunk")); n != 1 {
		t.Fatalf("%d file_chunk reçus, attendu 1", n)
	}
	assertResult(t, msgs, 0, nil)
}

func TestExecutor_WriteFile_PolicyAutoNeverPrompts(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "auto.txt")
	conn, recv := newLoopbackConn(t)

	confirm := &recordingConfirm{approve: false}
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), confirm.fn, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "write_file", map[string]any{
		"path":           path,
		"content_base64": base64.StdEncoding.EncodeToString([]byte("auto")),
	}))

	msgs := collectUntilResult(t, recv)
	if len(confirm.descriptions) != 0 {
		t.Errorf("confirm appelé sous PolicyAuto: %v", confirm.descriptions)
	}
	if n := len(messagesOfType(msgs, "approval_response")); n != 0 {
		t.Errorf("%d approval_response sous PolicyAuto, attendu 0", n)
	}
	assertResult(t, msgs, 0, nil)
	assertFileContent(t, path, []byte("auto"))
}

// Le code d'erreur stable du transfert doit remonter tel quel dans
// result.error, pour que le harnais puisse le traiter.
func TestExecutor_ReadFile_MissingFileReportsStableErrorCode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "absent.txt")
	conn, recv := newLoopbackConn(t)

	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))

	msgs := collectUntilResult(t, recv)
	res := singleMessage(t, msgs, "result")
	if res["error"] != "file_not_found" {
		t.Errorf("error = %v, want file_not_found", res["error"])
	}
	if res["exit_code"] == float64(0) {
		t.Error("exit_code = 0 alors que la lecture a échoué")
	}
}

func TestExecutor_WriteFile_InvalidBase64ReportsStableErrorCode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "out.txt")
	conn, recv := newLoopbackConn(t)

	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", nil)
	e.Handle(context.Background(), fileCommand(t, "r1", "write_file", map[string]any{
		"path":           path,
		"content_base64": "pas du base64 !!",
	}))

	msgs := collectUntilResult(t, recv)
	res := singleMessage(t, msgs, "result")
	if res["error"] != "invalid_base64" {
		t.Errorf("error = %v, want invalid_base64", res["error"])
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Error("un fichier a été créé malgré un base64 invalide")
	}
}

// --- PolicyController wiring: Executor must consult Get() on every decision ---
//
// Red-first tests for the PolicyController plumbing (Executor.policy is
// still a bare Policy at this point). Every
// NewExecutor(...) call above was mechanically adapted to
// NewExecutor(conn, NewPolicyController(policy), confirm, workDir, nil) —
// same assertions, new plumbing — so those are the non-regression: this
// section adds genuinely new behavior.

func TestExecutor_PolicyController_LiveChangeTakesEffectImmediately(t *testing.T) {
	controller := NewPolicyController(PolicyDeny)
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, controller, nil, "", nil)

	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": "/etc/hosts"}))
	assertResult(t, collectUntilResult(t, recv), 126, "refused_by_policy")

	// Flip the policy at runtime, exactly as the GUI's "mode automatique"
	// switch would (client/gui.go) — the very next decision must honor it
	// immediately, without recreating the Executor or restarting the
	// session.
	controller.Set(PolicyAuto)

	path := writeTempFile(t, t.TempDir(), "data.txt", []byte("x"))
	e.Handle(context.Background(), fileCommand(t, "r2", "read_file", map[string]any{"path": path}))
	msgs := collectUntilResult(t, recv)
	assertResult(t, msgs, 0, nil)
	if n := len(messagesOfType(msgs, "approval_response")); n != 0 {
		t.Errorf("%d approval_response after switching to PolicyAuto, want 0", n)
	}
}

// --- Executor event sink: emission for the GUI's live activity log ---
//
// eventRecorder is a minimal, concurrency-safe func(ActivityEvent) sink for
// tests. A mutex is used (rather than assuming single-threaded access) since
// production Executors can run several commands concurrently on separate
// goroutines (see main.go's runSession comment on why Handle runs in its
// own goroutine per command).
type eventRecorder struct {
	mu     sync.Mutex
	events []ActivityEvent
}

func (r *eventRecorder) record(e ActivityEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, e)
}

func (r *eventRecorder) all() []ActivityEvent {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]ActivityEvent, len(r.events))
	copy(out, r.events)
	return out
}

func (r *eventRecorder) kinds() []ActivityKind {
	var out []ActivityKind
	for _, e := range r.all() {
		out = append(out, e.Kind)
	}
	return out
}

func TestExecutor_Handle_EmitsCommandReceivedEventFirst(t *testing.T) {
	rec := &eventRecorder{}
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", rec.record)

	e.Handle(context.Background(), fileCommand(t, "r1", "system_info", map[string]any{}))
	collectUntilResult(t, recv)

	kinds := rec.kinds()
	if len(kinds) == 0 || kinds[0] != ActivityCommand {
		t.Fatalf("emitted kinds = %v, want ActivityCommand first", kinds)
	}
}

// The command event must carry the command itself, not just the tool that
// carried it: it is what the GUI's activity log shows the operator, and
// under the default `auto` policy nothing else on screen reveals what the
// harness asked for. See describeCommand (commanddetail.go).
func TestExecutor_Handle_CommandEventCarriesTheCommandItself(t *testing.T) {
	rec := &eventRecorder{}
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", rec.record)

	e.Handle(context.Background(), fileCommand(t, "r1", "run_shell", map[string]any{"command": "echo bonjour"}))
	collectUntilResult(t, recv)

	events := rec.all()
	if len(events) == 0 || events[0].Kind != ActivityCommand {
		t.Fatalf("first event = %+v, want an ActivityCommand", events)
	}
	if !strings.Contains(events[0].Detail, "echo bonjour") {
		t.Errorf("command event detail = %q, want it to carry the command text", events[0].Detail)
	}
}

func TestExecutor_ResolveApproval_EmitsRequestAndDecisionEvents(t *testing.T) {
	rec := &eventRecorder{}
	confirm := &recordingConfirm{approve: true}
	e := NewExecutor(nil, NewPolicyController(PolicyConfirm), confirm.fn, "", rec.record)

	e.resolveApproval("shutdown -h now")

	approvalCount := 0
	for _, k := range rec.kinds() {
		if k == ActivityApproval {
			approvalCount++
		}
	}
	if approvalCount != 2 {
		t.Errorf("emitted %d ActivityApproval events, want 2 (one request, one decision); kinds = %v", approvalCount, rec.kinds())
	}
}

func TestExecutor_ResolveApproval_MemoizedAlwaysDoesNotReemitOnSecondCall(t *testing.T) {
	rec := &eventRecorder{}
	confirm := &recordingConfirm{approve: true, always: true}
	e := NewExecutor(nil, NewPolicyController(PolicyConfirm), confirm.fn, "", rec.record)

	e.resolveApproval("shutdown -h now") // prompts once, remembers "toujours"
	before := len(rec.kinds())
	e.resolveApproval("shutdown -h now") // memoized: no re-prompt, no new event
	after := len(rec.kinds())
	if after != before {
		t.Errorf("second (memoized) resolveApproval emitted %d new events, want 0", after-before)
	}
}

func TestExecutor_ReadFile_EmitsFileReadEventOnSuccess(t *testing.T) {
	content := []byte("data")
	path := writeTempFile(t, t.TempDir(), "data.txt", content)
	rec := &eventRecorder{}
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", rec.record)

	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))
	collectUntilResult(t, recv)

	found := false
	for _, k := range rec.kinds() {
		if k == ActivityFileRead {
			found = true
		}
	}
	if !found {
		t.Errorf("no ActivityFileRead event emitted, kinds = %v", rec.kinds())
	}
}

func TestExecutor_WriteFile_EmitsFileWriteEventOnSuccess(t *testing.T) {
	path := filepath.Join(t.TempDir(), "out.txt")
	rec := &eventRecorder{}
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", rec.record)

	e.Handle(context.Background(), fileCommand(t, "r1", "write_file", map[string]any{
		"path":           path,
		"content_base64": base64.StdEncoding.EncodeToString([]byte("x")),
	}))
	collectUntilResult(t, recv)

	found := false
	for _, k := range rec.kinds() {
		if k == ActivityFileWrite {
			found = true
		}
	}
	if !found {
		t.Errorf("no ActivityFileWrite event emitted, kinds = %v", rec.kinds())
	}
}

func TestExecutor_EmitsResultEventCarryingExitCode(t *testing.T) {
	rec := &eventRecorder{}
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, NewPolicyController(PolicyDeny), nil, "", rec.record)

	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": "/etc/hosts"}))
	collectUntilResult(t, recv)

	found := false
	for _, ev := range rec.all() {
		if ev.Kind == ActivityResult && contains(ev.Detail, "126") {
			found = true
		}
	}
	if !found {
		t.Errorf("no ActivityResult event carrying exit code 126, events = %+v", rec.all())
	}
}

func TestExecutor_ReadFile_SuccessfulTransferAlsoEmitsResultEvent(t *testing.T) {
	// Success results are written via NewResultMessageWithMeta directly
	// (not through the plain sendResult path used by run_shell/run_command),
	// so this pins that both routes reach the event sink.
	content := []byte("data")
	path := writeTempFile(t, t.TempDir(), "data.txt", content)
	rec := &eventRecorder{}
	conn, recv := newLoopbackConn(t)
	e := NewExecutor(conn, NewPolicyController(PolicyAuto), nil, "", rec.record)

	e.Handle(context.Background(), fileCommand(t, "r1", "read_file", map[string]any{"path": path}))
	collectUntilResult(t, recv)

	found := false
	for _, ev := range rec.all() {
		if ev.Kind == ActivityResult {
			found = true
		}
	}
	if !found {
		t.Errorf("no ActivityResult event emitted for a successful read_file, events = %+v", rec.all())
	}
}

// Non-regression #2 (mandated by the task spec): a nil events sink —
// the default for every pre-existing caller, including main.go's console
// entry point — must not change the Executor's behavior in any way. The 13
// mechanically-adapted NewExecutor(...) calls elsewhere in this file (all
// passing nil for events, unchanged assertions) are the broad version of
// this pin; this test exercises the guard-rail path directly.
func TestExecutor_NilEventsSinkDoesNotChangeApprovalBehavior(t *testing.T) {
	confirm := &recordingConfirm{approve: true}
	e := NewExecutor(nil, NewPolicyController(PolicyConfirm), confirm.fn, "", nil)
	if !e.resolveApproval("anything") {
		t.Fatal("resolveApproval with a nil events sink behaved differently than with one attached")
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
