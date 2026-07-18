package main

import "testing"

// Red-first tests for the panic-safe shutdown guard (lifecycle.go does not
// exist yet): cleanup must run exactly once whether fn returns normally or
// panics, and a panic must still propagate afterward (cleanup must never
// silently swallow a real bug) — this is what guarantees the workspace
// directory (workspace.go) is removed "even on panic" as required by
// docs/PLAN.md Phase 6.

func TestRunGuarded_CleanupRunsOnNormalReturn(t *testing.T) {
	called := false
	RunGuarded(func() { called = true }, func() {})
	if !called {
		t.Error("cleanup was not called after a normal return")
	}
}

func TestRunGuarded_CleanupRunsBeforePanicPropagates(t *testing.T) {
	cleaned := false

	func() {
		defer func() {
			r := recover()
			if r == nil {
				t.Error("expected the panic to propagate out of RunGuarded")
			}
			if r != "boom" {
				t.Errorf("recovered value = %v, want %q", r, "boom")
			}
			if !cleaned {
				t.Error("cleanup did not run before the panic propagated")
			}
		}()
		RunGuarded(func() { cleaned = true }, func() { panic("boom") })
	}()
}

func TestRunGuarded_CleanupRunsExactlyOnce(t *testing.T) {
	count := 0
	RunGuarded(func() { count++ }, func() {})
	if count != 1 {
		t.Errorf("cleanup ran %d times, want exactly 1", count)
	}
}
