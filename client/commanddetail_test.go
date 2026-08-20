package main

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"
)

// cmdMsg builds a `command` message with params given as raw JSON, so these
// tests exercise describeCommand on exactly the bytes the relay would put on
// the wire (including shapes no typed struct could produce, like a malformed
// params blob).
func cmdMsg(tool, requestID, params string) CommandMessage {
	return CommandMessage{Type: TypeCommand, RequestID: requestID, Tool: tool, Params: json.RawMessage(params)}
}

func TestDescribeCommand_ShowsShellCommandAndRequestID(t *testing.T) {
	got := describeCommand(cmdMsg("run_shell", "r-1", `{"command":"systemctl status nginx"}`))

	// The whole point of the feature: the operator reads the command itself
	// in the window, not just the tool that carried it.
	if !strings.Contains(got, "systemctl status nginx") {
		t.Errorf("describeCommand = %q, want it to contain the command", got)
	}
	if !strings.Contains(got, "run_shell") {
		t.Errorf("describeCommand = %q, want it to contain the tool name", got)
	}
	if !strings.Contains(got, "r-1") {
		t.Errorf("describeCommand = %q, want it to contain the request id", got)
	}
}

func TestDescribeCommand_CollapsesMultilineScriptToOneLine(t *testing.T) {
	script := "echo un\r\n\techo deux\n\n echo trois "
	got := describeCommand(cmdMsg("run_shell", "r-2", mustJSON(t, map[string]any{"command": script})))

	if strings.ContainsAny(got, "\r\n\t") {
		t.Errorf("describeCommand = %q, want a single line (no CR/LF/TAB)", got)
	}
	// A line break must stay visible as such: collapsed to a plain space,
	// two commands would read as one command with extra arguments.
	if !strings.Contains(got, `echo un \n echo deux \n echo trois`) {
		t.Errorf("describeCommand = %q, want line breaks marked and other whitespace collapsed", got)
	}
}

func TestDescribeCommand_ShowsShellAndTimeoutForRunShell(t *testing.T) {
	got := describeCommand(cmdMsg("run_shell", "r-3", `{"command":"ls","shell":"sh","timeout":42}`))

	if !strings.Contains(got, "shell=sh") {
		t.Errorf("describeCommand = %q, want the shell override", got)
	}
	if !strings.Contains(got, "timeout=42s") {
		t.Errorf("describeCommand = %q, want the timeout", got)
	}
}

// run_command never spawns an interpreter (executor.go:buildPlainCommand),
// so echoing a shell override there would advertise a choice the client does
// not make.
func TestDescribeCommand_IgnoresShellForRunCommand(t *testing.T) {
	got := describeCommand(cmdMsg("run_command", "r-4", `{"command":"uname -a","shell":"bash"}`))

	if strings.Contains(got, "shell=") {
		t.Errorf("describeCommand = %q, want no shell mention for run_command", got)
	}
	if !strings.Contains(got, "uname -a") {
		t.Errorf("describeCommand = %q, want the command", got)
	}
}

func TestDescribeCommand_ShowsReadFilePathAndRange(t *testing.T) {
	got := describeCommand(cmdMsg("read_file", "r-5", `{"path":"/etc/hosts","offset":128,"max_bytes":4096}`))

	for _, want := range []string{"read_file", "/etc/hosts", "offset=128", "max_bytes=4096"} {
		if !strings.Contains(got, want) {
			t.Errorf("describeCommand = %q, want it to contain %q", got, want)
		}
	}
}

// The payload of a write_file is never logged — only its size — mirroring
// the relay's audit redaction (relay/broker.py:_redact_params). A log that
// copied the data would be a second place for it to leak from.
func TestDescribeCommand_WriteFileShowsSizeNeverContent(t *testing.T) {
	// "secret-payload" base64-encoded.
	got := describeCommand(cmdMsg("write_file", "r-6", `{"path":"/tmp/app.conf","content_base64":"c2VjcmV0LXBheWxvYWQ="}`))

	if strings.Contains(got, "c2VjcmV0LXBheWxvYWQ=") {
		t.Errorf("describeCommand = %q, must not log the file content", got)
	}
	if !strings.Contains(got, "/tmp/app.conf") {
		t.Errorf("describeCommand = %q, want the target path", got)
	}
	if !strings.Contains(got, "14 octets") {
		t.Errorf("describeCommand = %q, want the decoded payload size", got)
	}
}

func TestDescribeCommand_SystemInfoHasNoParamsNoise(t *testing.T) {
	got := describeCommand(cmdMsg("system_info", "r-7", `{}`))

	if got != "system_info (request r-7)" {
		t.Errorf("describeCommand = %q, want a bare tool + request id line", got)
	}
}

func TestDescribeCommand_MissingParamsStillReadable(t *testing.T) {
	got := describeCommand(CommandMessage{Type: TypeCommand, RequestID: "r-8", Tool: "run_shell"})

	if got != "run_shell (request r-8)" {
		t.Errorf("describeCommand = %q, want a bare tool + request id line", got)
	}
}

// An unknown tool (executor.go answers it with "outil inconnu") is exactly
// when the operator most needs to see what actually arrived.
func TestDescribeCommand_UnknownToolShowsRawParams(t *testing.T) {
	got := describeCommand(cmdMsg("do_something_new", "r-9", `{ "target" : "/etc" }`))

	if !strings.Contains(got, `"target":"/etc"`) {
		t.Errorf("describeCommand = %q, want the raw params compacted onto the line", got)
	}
}

func TestDescribeCommand_MalformedParamsFallBackToRaw(t *testing.T) {
	got := describeCommand(cmdMsg("run_shell", "r-10", `{"command": `))

	if !strings.Contains(got, "command") {
		t.Errorf("describeCommand = %q, want the undecodable params shown rather than dropped", got)
	}
	if strings.ContainsAny(got, "\r\n") {
		t.Errorf("describeCommand = %q, want a single line", got)
	}
}

func TestDescribeCommand_TruncatesVeryLongCommand(t *testing.T) {
	long := strings.Repeat("a", maxLoggedDetailRunes+250)
	got := describeCommand(cmdMsg("run_shell", "r-11", mustJSON(t, map[string]any{"command": long})))

	if utf8.RuneCountInString(got) > maxLoggedDetailRunes+120 {
		t.Errorf("describeCommand kept %d runes, want the command truncated near %d", utf8.RuneCountInString(got), maxLoggedDetailRunes)
	}
	if !strings.Contains(got, "+250 caractères") {
		t.Errorf("describeCommand = %q, want an explicit marker of how much was cut", got)
	}
}

// Truncation counts runes, not bytes: cutting mid-rune would corrupt the
// accented text and UTF-8 paths this client handles routinely.
func TestSanitizeLogLine_TruncationIsRuneSafe(t *testing.T) {
	got := sanitizeLogLine(strings.Repeat("é", maxLoggedDetailRunes+10))

	if !utf8.ValidString(got) {
		t.Errorf("sanitizeLogLine produced invalid UTF-8: %q", got)
	}
	if !strings.HasPrefix(got, strings.Repeat("é", maxLoggedDetailRunes)) {
		t.Error("sanitizeLogLine truncated mid-rune or dropped valid runes")
	}
}

// A stray escape sequence in a command must not reach the terminal that
// renders the saved log file.
func TestSanitizeLogLine_DropsControlCharacters(t *testing.T) {
	got := sanitizeLogLine("avant\x1b[31mrouge\x00après")

	if strings.ContainsAny(got, "\x1b\x00") {
		t.Errorf("sanitizeLogLine = %q, want control characters removed", got)
	}
	if !strings.Contains(got, "avant") || !strings.Contains(got, "après") {
		t.Errorf("sanitizeLogLine = %q, want the printable text preserved", got)
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal params: %v", err)
	}
	return string(b)
}
