package main

import (
	"strings"
	"sync"
	"testing"
	"time"
)

// Red-first tests for ActivityLog (activitylog.go does not exist yet).
// It has no GUI dependency and is useful and testable on its own: the
// Executor (executor.go) appends to it, and the
// GUI (client/gui.go, out of this task's scope) will read/subscribe to it
// from Fyne's own goroutine while the Executor writes from another —
// exercised below with `go test -race`.

func TestActivityLog_AppendAndEvents_PreservesChronologicalOrder(t *testing.T) {
	l := NewActivityLog(10)
	l.Append(ActivityEvent{Time: time.Unix(1, 0), Kind: ActivityCommand, Detail: "first"})
	l.Append(ActivityEvent{Time: time.Unix(2, 0), Kind: ActivityResult, Detail: "second"})
	l.Append(ActivityEvent{Time: time.Unix(3, 0), Kind: ActivityApproval, Detail: "third"})

	got := l.Events()
	if len(got) != 3 {
		t.Fatalf("len(Events()) = %d, want 3", len(got))
	}
	wantDetails := []string{"first", "second", "third"}
	for i, want := range wantDetails {
		if got[i].Detail != want {
			t.Errorf("Events()[%d].Detail = %q, want %q", i, got[i].Detail, want)
		}
	}
}

func TestActivityLog_Events_ReturnsACopy(t *testing.T) {
	l := NewActivityLog(10)
	l.Append(ActivityEvent{Kind: ActivityCommand, Detail: "one"})

	got := l.Events()
	got[0].Detail = "mutated"

	again := l.Events()
	if again[0].Detail != "one" {
		t.Errorf("Events() returned a live view instead of a copy: got %q after mutating the first copy", again[0].Detail)
	}
}

func TestActivityLog_BoundedCapacity_EvictsOldestFirst(t *testing.T) {
	l := NewActivityLog(3)
	for i := 0; i < 5; i++ {
		l.Append(ActivityEvent{Detail: string(rune('a' + i))})
	}
	got := l.Events()
	if len(got) != 3 {
		t.Fatalf("len(Events()) = %d, want capacity 3 after 5 appends", len(got))
	}
	wantDetails := []string{"c", "d", "e"} // the two oldest ("a", "b") were evicted
	for i, want := range wantDetails {
		if got[i].Detail != want {
			t.Errorf("Events()[%d].Detail = %q, want %q (oldest-first eviction)", i, got[i].Detail, want)
		}
	}
}

func TestActivityLog_Render_ProducesTimestampedText(t *testing.T) {
	l := NewActivityLog(10)
	ts := time.Date(2026, 8, 6, 10, 30, 0, 0, time.UTC)
	l.Append(ActivityEvent{Time: ts, Kind: ActivityCommand, Detail: "run_shell df -h"})

	rendered := l.Render()
	if !strings.Contains(rendered, "run_shell df -h") {
		t.Errorf("Render() = %q, want it to contain the event detail", rendered)
	}
	if !strings.Contains(rendered, string(ActivityCommand)) {
		t.Errorf("Render() = %q, want it to contain the event kind", rendered)
	}
	if !strings.Contains(rendered, "2026") {
		t.Errorf("Render() = %q, want it to contain a timestamp", rendered)
	}
}

func TestActivityLog_Render_EmptyLogIsEmptyString(t *testing.T) {
	l := NewActivityLog(10)
	if got := l.Render(); got != "" {
		t.Errorf("Render() on an empty log = %q, want \"\"", got)
	}
}

func TestActivityLog_Subscribe_NotifiesOnAppend(t *testing.T) {
	l := NewActivityLog(10)
	var mu sync.Mutex
	var received []ActivityEvent
	l.Subscribe(func(e ActivityEvent) {
		mu.Lock()
		defer mu.Unlock()
		received = append(received, e)
	})

	l.Append(ActivityEvent{Kind: ActivityConnection, Detail: "connecté"})
	l.Append(ActivityEvent{Kind: ActivityResult, Detail: "exit=0"})

	mu.Lock()
	defer mu.Unlock()
	if len(received) != 2 {
		t.Fatalf("subscriber received %d events, want 2", len(received))
	}
	if received[0].Detail != "connecté" || received[1].Detail != "exit=0" {
		t.Errorf("subscriber received %v, want [connecté, exit=0] in order", received)
	}
}

func TestActivityLog_Subscribe_MultipleSubscribersAllNotified(t *testing.T) {
	l := NewActivityLog(10)
	var mu sync.Mutex
	countA, countB := 0, 0
	l.Subscribe(func(ActivityEvent) { mu.Lock(); countA++; mu.Unlock() })
	l.Subscribe(func(ActivityEvent) { mu.Lock(); countB++; mu.Unlock() })

	l.Append(ActivityEvent{Detail: "x"})

	mu.Lock()
	defer mu.Unlock()
	if countA != 1 || countB != 1 {
		t.Errorf("countA=%d countB=%d, want both subscribers notified once", countA, countB)
	}
}

// TestActivityLog_ConcurrentAppendAndRead exercises ActivityLog the way the
// real client will use it: the Executor appending from one (or several,
// since multiple commands run concurrently — see main.go's runSession)
// goroutine while the GUI's own goroutine calls Events()/Render()
// concurrently. Run under `go test -race` (mandated by the task).
func TestActivityLog_ConcurrentAppendAndRead(t *testing.T) {
	l := NewActivityLog(500)
	var wg sync.WaitGroup

	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				l.Append(ActivityEvent{Time: time.Now(), Kind: ActivityCommand, Detail: "event"})
			}
		}(i)
	}
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				_ = l.Events()
				_ = l.Render()
			}
		}()
	}
	wg.Wait()
}

// --- Non-regression: nil-safety ---
//
// A *ActivityLog obtained as a nil pointer (e.g. an Executor built before
// the GUI wires one in) must not panic — mirrors Workspace's nil-safe
// Dir()/Path()/Cleanup() (workspace.go).

func TestActivityLog_NilSafe(t *testing.T) {
	var l *ActivityLog
	l.Append(ActivityEvent{Detail: "should not panic"})
	if got := l.Events(); got != nil {
		t.Errorf("(*ActivityLog)(nil).Events() = %v, want nil", got)
	}
	if got := l.Render(); got != "" {
		t.Errorf("(*ActivityLog)(nil).Render() = %q, want \"\"", got)
	}
	l.Subscribe(func(ActivityEvent) {})
}
