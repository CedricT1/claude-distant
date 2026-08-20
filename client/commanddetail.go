package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

// maxLoggedDetailRunes bounds how much of a command a single activity line
// carries. It is deliberately generous (an administration one-liner fits
// whole) but finite: the harness can legitimately send a multi-kilobyte
// PowerShell script, and one such command must not push the rest of the
// session's log out of the GUI's bounded ActivityLog — nor turn the saved
// log into an unreadable wall of text. Anything longer is cut with an
// explicit marker saying how much was dropped, so a truncated line is never
// mistaken for the whole command.
const maxLoggedDetailRunes = 400

// describeCommand renders one inbound `command` message as a single
// human-readable line for the GUI's activity log (client/gui.go). This is
// what makes the window answer "what is the harness doing right now?": the
// tool name alone (all this event used to carry) says a run_shell happened,
// not *which* command ran — and under the default `auto` policy no
// confirmation dialog shows it either, so the log is the only place the
// operator can see it.
//
// The shape is "<outil> : <résumé> (request <id>)", the request id last
// because it only matters when correlating with the relay's audit trail.
// Every rendered fragment goes through sanitizeLogLine, so one event is
// always exactly one line — the invariant both the live list and
// ActivityLog.Render()'s saved file rely on.
func describeCommand(cmd CommandMessage) string {
	line := cmd.Tool
	if line == "" {
		line = "(outil non précisé)"
	}
	if summary := describeToolParams(cmd.Tool, cmd.Params); summary != "" {
		line += " : " + summary
	}
	return fmt.Sprintf("%s (request %s)", line, cmd.RequestID)
}

// describeToolParams summarizes a tool's params the way that tool's operator
// would want to read them: the command line for run_shell/run_command, the
// path (and payload size) for the file transfers. It returns "" when there
// is nothing worth showing — system_info takes no parameters, and neither
// does a command message that carries none at all.
//
// It never returns a decoding error: a params blob this client cannot make
// sense of still gets logged (raw and truncated, see rawParamsSummary)
// rather than silently reduced to a bare tool name. The tool handlers in
// executor.go are the ones that reject malformed params with an
// `invalid_params` result; this function's only job is to show the operator
// what arrived.
func describeToolParams(tool string, params json.RawMessage) string {
	if len(bytes.TrimSpace(params)) == 0 {
		return ""
	}

	switch tool {
	case "run_shell", "run_command":
		var p RunParams
		if err := json.Unmarshal(params, &p); err != nil {
			return rawParamsSummary(params)
		}
		summary := sanitizeLogLine(p.Command)
		if summary == "" {
			summary = "(commande vide)"
		}
		var extras []string
		// Shell is meaningful for run_shell only: run_command never spawns
		// an interpreter, so echoing a shell override there would suggest a
		// choice the client does not actually make.
		if shell := strings.TrimSpace(p.Shell); shell != "" && tool == "run_shell" {
			extras = append(extras, "shell="+sanitizeLogLine(shell))
		}
		if p.Timeout > 0 {
			extras = append(extras, fmt.Sprintf("timeout=%ds", p.Timeout))
		}
		if len(extras) > 0 {
			summary += " [" + strings.Join(extras, ", ") + "]"
		}
		return summary

	case "read_file":
		var p ReadFileParams
		if err := json.Unmarshal(params, &p); err != nil {
			return rawParamsSummary(params)
		}
		summary := pathSummary(p.Path)
		var extras []string
		if p.Offset > 0 {
			extras = append(extras, fmt.Sprintf("offset=%d", p.Offset))
		}
		if p.MaxBytes > 0 {
			extras = append(extras, fmt.Sprintf("max_bytes=%d", p.MaxBytes))
		}
		if len(extras) > 0 {
			summary += " [" + strings.Join(extras, ", ") + "]"
		}
		return summary

	case "write_file":
		var p WriteFileParams
		if err := json.Unmarshal(params, &p); err != nil {
			return rawParamsSummary(params)
		}
		// The payload itself is never logged, only its size — same choice
		// as the relay's audit trail, which redacts content_base64
		// (relay/broker.py:_redact_params): the log proves what was written
		// where, without becoming a copy of the data.
		summary := fmt.Sprintf("%s (%d octets)", pathSummary(p.Path), base64DecodedLen(p.ContentBase64))
		var extras []string
		if p.CreateDirs {
			extras = append(extras, "create_dirs")
		}
		if p.Overwrite != nil && !*p.Overwrite {
			extras = append(extras, "overwrite=false")
		}
		if mode := strings.TrimSpace(p.Mode); mode != "" {
			extras = append(extras, "mode="+sanitizeLogLine(mode))
		}
		if len(extras) > 0 {
			summary += " [" + strings.Join(extras, ", ") + "]"
		}
		return summary

	case "system_info":
		// Takes no parameters; anything sent alongside is noise, not
		// something the operator needs to read.
		return ""

	default:
		// An unknown tool is exactly the case where the operator most needs
		// to see what arrived (executor.go answers it with "outil inconnu"),
		// so show the raw params rather than nothing.
		return rawParamsSummary(params)
	}
}

// pathSummary renders a file-transfer path, naming the missing-path case
// explicitly instead of rendering an empty string the operator would read as
// a display bug.
func pathSummary(path string) string {
	if s := sanitizeLogLine(path); s != "" {
		return s
	}
	return "(chemin manquant)"
}

// rawParamsSummary is the fallback for params this client has no typed shape
// for (unknown tool) or cannot decode at all (malformed JSON): show them
// compacted onto one line, truncated like everything else.
func rawParamsSummary(params json.RawMessage) string {
	var compact bytes.Buffer
	if err := json.Compact(&compact, params); err == nil {
		return sanitizeLogLine(compact.String())
	}
	return sanitizeLogLine(string(params))
}

// sanitizeLogLine folds an arbitrary string from the harness into something
// safe to append to a one-line log entry: every whitespace run (newlines and
// tabs of a multi-line script included) collapses to a single space, other
// control characters — which could otherwise garble a terminal or the saved
// log file — are dropped, and the result is truncated to
// maxLoggedDetailRunes with a marker stating how many characters were cut.
func sanitizeLogLine(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	pendingSpace := false
	for _, r := range s {
		switch {
		case unicode.IsSpace(r):
			// Collapse instead of emitting: leading whitespace is dropped
			// (nothing written yet) and trailing whitespace never gets
			// flushed, so no Trim pass is needed afterwards.
			pendingSpace = b.Len() > 0
		case r == utf8.RuneError || unicode.IsControl(r):
			// Skip: not printable, and a stray CSI sequence must not reach
			// a terminal that renders the saved log.
		default:
			if pendingSpace {
				b.WriteRune(' ')
				pendingSpace = false
			}
			b.WriteRune(r)
		}
	}
	return truncateRunes(b.String(), maxLoggedDetailRunes)
}

// truncateRunes cuts s to at most limit runes (never mid-rune, which
// counting bytes would risk on the accented French text and UTF-8 paths this
// client handles routinely), appending a marker that states how much was
// dropped.
func truncateRunes(s string, limit int) string {
	if limit <= 0 || utf8.RuneCountInString(s) <= limit {
		return s
	}
	runes := []rune(s)
	return string(runes[:limit]) + fmt.Sprintf("… (+%d caractères)", len(runes)-limit)
}
