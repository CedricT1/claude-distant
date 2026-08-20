package main

import (
	"fmt"
	"strings"
	"sync"
	"time"
)

// ActivityKind categorizes one ActivityEvent, mirroring the operations the
// Executor performs on behalf of the harness.
type ActivityKind string

const (
	ActivityConnection ActivityKind = "connection"
	ActivityCommand    ActivityKind = "command"
	ActivityFileRead   ActivityKind = "file_read"
	ActivityFileWrite  ActivityKind = "file_write"
	ActivityApproval   ActivityKind = "approval"
	ActivityResult     ActivityKind = "result"
)

// ActivityEvent is one entry in the ActivityLog, timestamped by the caller
// at the moment it happened (not by Append, so a caller can batch or
// reorder if it ever needs to — today nobody does).
type ActivityEvent struct {
	Time   time.Time
	Kind   ActivityKind
	Detail string
}

// ActivityLog is a bounded, thread-safe, in-memory activity journal. It has
// no GUI dependency: the Executor (executor.go) appends to it regardless of
// which UI is attached (or none, in console mode), and it stands on its own
// as prerequisite plumbing for the GUI's live log panel (client/gui.go,
// out of this task's scope).
//
// It is a fixed-capacity circular buffer: a long-running session must not
// grow memory without bound, so once capacity is reached, appending evicts
// the oldest entry. All exported methods are nil-safe (mirrors Workspace's
// nil-safe Dir()/Path()/Cleanup(), workspace.go) so an Executor built before
// a log exists never has to special-case it, and safe for concurrent use:
// the Executor appends from one or more command goroutines while the GUI
// reads/subscribes from its own goroutine (verified with `go test -race`).
type ActivityLog struct {
	mu       sync.Mutex
	events   []ActivityEvent
	capacity int

	subMu       sync.Mutex
	subscribers []func(ActivityEvent)
}

// NewActivityLog creates an ActivityLog holding at most capacity events
// (docs/PROTOCOL.md suggests 5000 for a session-long log). capacity <=
// 0 is treated as 1 rather than being rejected outright, since "keep
// nothing" isn't a useful log and a caller-provided constant is unlikely to
// be validated elsewhere.
func NewActivityLog(capacity int) *ActivityLog {
	if capacity <= 0 {
		capacity = 1
	}
	return &ActivityLog{capacity: capacity}
}

// Append records e, evicting the oldest event first if the log is already
// at capacity, then notifies every subscriber (see Subscribe). Safe to call
// on a nil receiver (no-op).
func (l *ActivityLog) Append(e ActivityEvent) {
	if l == nil {
		return
	}
	l.mu.Lock()
	l.events = append(l.events, e)
	if over := len(l.events) - l.capacity; over > 0 {
		// Copy-and-reslice (rather than ring-buffer index arithmetic) keeps
		// this simple and obviously correct; ActivityLog's capacity is
		// modest (thousands of entries for a whole session), so this isn't
		// a hot path worth the extra complexity.
		l.events = append(l.events[:0:0], l.events[over:]...)
	}
	l.mu.Unlock()

	l.notify(e)
}

// Events returns a snapshot copy of all currently retained events, oldest
// first. Mutating the returned slice never affects the log. Safe to call on
// a nil receiver (returns nil).
func (l *ActivityLog) Events() []ActivityEvent {
	if l == nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]ActivityEvent, len(l.events))
	copy(out, l.events)
	return out
}

// Render produces the exact text saved by the GUI's "Enregistrer le log…"
// button (client/gui.go): one RFC3339-timestamped line per event, oldest
// first. An empty log renders as the empty string. Safe to call on a nil
// receiver.
func (l *ActivityLog) Render() string {
	events := l.Events()
	if len(events) == 0 {
		return ""
	}
	var b strings.Builder
	for _, e := range events {
		b.WriteString(renderActivityLineFull(e))
		b.WriteByte('\n')
	}
	return b.String()
}

// renderActivityLineFull renders one event with its absolute RFC3339
// timestamp: the form used wherever the line has to stand on its own —
// the saved log file (Render, above) and the GUI's per-entry detail dialog
// (client/gui.go:showLogEntry), both of which can be read days later or
// pasted into a report. The live panel abbreviates the same line to the
// time of day, where horizontal space is scarce and the date is whatever
// today is; see renderActivityLine (client/gui.go).
func renderActivityLineFull(e ActivityEvent) string {
	return fmt.Sprintf("%s [%s] %s", e.Time.Format(time.RFC3339), e.Kind, e.Detail)
}

// Subscribe registers fn to be called, from within Append, every time a new
// event is recorded — how the GUI keeps its live log panel in sync without
// polling. fn is invoked synchronously on the appending goroutine's call to
// Append, so it must not block or itself call back into this ActivityLog.
// Safe to call on a nil receiver (no-op) or with a nil fn (ignored).
func (l *ActivityLog) Subscribe(fn func(ActivityEvent)) {
	if l == nil || fn == nil {
		return
	}
	l.subMu.Lock()
	l.subscribers = append(l.subscribers, fn)
	l.subMu.Unlock()
}

// notify calls every subscriber with e. The subscriber list is copied under
// subMu and then invoked without holding the lock, so a slow or
// re-entrant subscriber can't block Subscribe/Append on other goroutines.
func (l *ActivityLog) notify(e ActivityEvent) {
	l.subMu.Lock()
	subs := make([]func(ActivityEvent), len(l.subscribers))
	copy(subs, l.subscribers)
	l.subMu.Unlock()

	for _, fn := range subs {
		fn(e)
	}
}
