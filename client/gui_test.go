//go:build gui

package main

import (
	"strings"
	"testing"
	"time"

	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"
)

// These tests only build under `-tags gui` (like gui.go itself) and use
// Fyne's headless test driver, so they need no display. They cover the log
// panel's own logic — which events it shows and in what order — not Fyne's
// rendering.

func newTestGUIState(t *testing.T) *guiState {
	t.Helper()
	a := test.NewApp()
	// Fyne's default *test* theme defines no bold-monospace face, which the
	// header's session code uses (gui.go:build) — measuring it would panic
	// inside the painter. The real theme has every face, so run these tests
	// against it.
	a.Settings().SetTheme(theme.DefaultTheme())
	t.Cleanup(a.Quit)
	w := a.NewWindow("test")
	t.Cleanup(w.Close)

	g := newGUIState(w, NewPolicyController(PolicyAuto), NewActivityLog(activityLogCapacity))
	g.build(true)
	return g
}

func commandEvent(detail string) ActivityEvent {
	return ActivityEvent{Time: time.Now(), Kind: ActivityCommand, Detail: detail}
}

func TestGUILog_ShowsEveryEventUnfiltered(t *testing.T) {
	g := newTestGUIState(t)

	g.appendLogEvent(commandEvent("run_shell : uname -a (request r-1)"))
	g.appendLogEvent(ActivityEvent{Time: time.Now(), Kind: ActivityResult, Detail: "request r-1: exit=0"})

	if len(g.logLines) != 2 {
		t.Fatalf("logLines = %v, want both events displayed", g.logLines)
	}
	if !strings.Contains(g.logLines[0], "uname -a") {
		t.Errorf("first line = %q, want the command text", g.logLines[0])
	}
}

func TestGUILog_CommandsOnlyFilterHidesTheRest(t *testing.T) {
	g := newTestGUIState(t)

	g.appendLogEvent(ActivityEvent{Time: time.Now(), Kind: ActivityConnection, Detail: "connecté"})
	g.appendLogEvent(commandEvent("run_shell : systemctl restart nginx (request r-2)"))
	g.appendLogEvent(ActivityEvent{Time: time.Now(), Kind: ActivityResult, Detail: "request r-2: exit=0"})

	g.commandsOnly = true
	g.rebuildLogLines()

	if len(g.logLines) != 1 {
		t.Fatalf("filtered logLines = %v, want only the harness command", g.logLines)
	}
	if !strings.Contains(g.logLines[0], "systemctl restart nginx") {
		t.Errorf("filtered line = %q, want the command", g.logLines[0])
	}

	// Filtering is a view, never a loss: everything is still recorded and
	// comes back when the box is unticked.
	g.commandsOnly = false
	g.rebuildLogLines()
	if len(g.logLines) != 3 {
		t.Fatalf("logLines after unfiltering = %v, want all three events back", g.logLines)
	}
}

// A command arriving while the filter is on must appear immediately; one
// that the filter hides must not.
func TestGUILog_AppendRespectsActiveFilter(t *testing.T) {
	g := newTestGUIState(t)
	g.commandsOnly = true

	g.appendLogEvent(ActivityEvent{Time: time.Now(), Kind: ActivityApproval, Detail: "demande: rm -rf /tmp/x"})
	if len(g.logLines) != 0 {
		t.Fatalf("logLines = %v, want a non-command event hidden by the filter", g.logLines)
	}

	g.appendLogEvent(commandEvent("read_file : /etc/hosts (request r-3)"))
	if len(g.logLines) != 1 {
		t.Fatalf("logLines = %v, want the command displayed", g.logLines)
	}
}

// The window mirrors the ActivityLog's bound: a long session must not grow
// the window's own copy without limit.
func TestGUILog_EvictsOldestBeyondCapacity(t *testing.T) {
	g := newTestGUIState(t)

	for i := 0; i < activityLogCapacity+5; i++ {
		g.appendLogEvent(commandEvent("commande"))
	}

	if len(g.logEvents) != activityLogCapacity {
		t.Errorf("logEvents = %d entries, want the log capped at %d", len(g.logEvents), activityLogCapacity)
	}
	if len(g.logLines) != activityLogCapacity {
		t.Errorf("logLines = %d entries, want %d", len(g.logLines), activityLogCapacity)
	}
}

// The rows the list renders and the events the detail dialog opens must stay
// index-aligned, filter on or off — a mismatch would show the operator one
// command and copy out another.
func TestGUILog_ShownEventsStayAlignedWithLines(t *testing.T) {
	g := newTestGUIState(t)
	g.appendLogEvent(ActivityEvent{Time: time.Now(), Kind: ActivityConnection, Detail: "connecté"})
	g.appendLogEvent(commandEvent("run_shell : df -h (request r-9)"))

	for _, commandsOnly := range []bool{false, true} {
		g.commandsOnly = commandsOnly
		g.rebuildLogLines()
		if len(g.logShown) != len(g.logLines) {
			t.Fatalf("commandsOnly=%v: %d shown events vs %d lines", commandsOnly, len(g.logShown), len(g.logLines))
		}
		for i, e := range g.logShown {
			if !strings.Contains(g.logLines[i], e.Detail) {
				t.Errorf("commandsOnly=%v: line %d = %q, want the detail of %q", commandsOnly, i, g.logLines[i], e.Detail)
			}
		}
	}
}

// The detail dialog is opened from a tap on a row; an index that no longer
// exists (filter toggled, eviction) must be a no-op, not a panic.
func TestGUILog_ShowLogEntryToleratesStaleIndex(t *testing.T) {
	g := newTestGUIState(t)
	g.appendLogEvent(commandEvent("run_shell : ls (request r-4)"))

	g.showLogEntry(-1)
	g.showLogEntry(len(g.logLines))
}
