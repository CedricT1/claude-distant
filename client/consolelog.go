package main

import (
	"fmt"
	"io"
	"sync"
)

// ConsoleActivityLog prints the Executor's activity to the terminal, so the
// console client shows what the harness is doing exactly like the GUI's log
// panel does (client/gui.go). It exists for the same reason: under the
// default `auto` policy nothing else on screen names the command that just
// ran — no confirmation prompt, and the command's own stdout/stderr goes to
// the relay, not to this terminal.
//
// It is the console counterpart of ActivityLog: the Executor takes any
// func(ActivityEvent) sink (executor.go), so the GUI feeds a bounded
// in-memory journal it can render and save, while this one simply writes
// each event out as it happens and keeps nothing.
type ConsoleActivityLog struct {
	// mu serializes writes: several commands can run concurrently (main.go
	// runs each Handle in its own goroutine), and an interleaved write
	// would garble the line — including against the confirm prompt, which
	// writes to this same terminal from yet another goroutine.
	mu sync.Mutex
	w  io.Writer
}

// NewConsoleActivityLog builds a console sink writing to w (os.Stdout in
// production, alongside the session code and the confirm prompt).
func NewConsoleActivityLog(w io.Writer) *ConsoleActivityLog {
	return &ConsoleActivityLog{w: w}
}

// Append prints one event as a single line, in the same compact format the
// GUI's live panel uses (renderActivityLine, activitylog.go), so an operator
// switching between the two variants reads the same thing. Its signature
// matches the Executor's events sink — pass it straight to NewExecutor.
//
// Write errors are ignored on purpose: a client whose stdout is closed or
// full (piped to a dead process) must keep serving the session rather than
// fail on a journal line.
func (c *ConsoleActivityLog) Append(e ActivityEvent) {
	if c == nil || c.w == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	fmt.Fprintln(c.w, renderActivityLine(e))
}
