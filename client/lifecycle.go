package main

// RunGuarded executes fn, guaranteeing that cleanup runs exactly once
// afterward — whether fn returns normally or panics. This is what backs
// the "sans trace" guarantee (docs/PLAN.md Phase 6): the scratch
// Workspace (workspace.go) must be removed even if something inside fn
// panics, not just on the happy path or a clean signal-triggered exit.
//
// If fn panics, the panic is recovered just long enough to run cleanup
// and is then re-raised, so callers (or, absent any other recovery, the Go
// runtime's default crash-with-stack-trace behavior) still observe it —
// cleanup must never silently swallow a genuine bug.
func RunGuarded(cleanup func(), fn func()) {
	defer func() {
		cleanup()
		if r := recover(); r != nil {
			panic(r)
		}
	}()
	fn()
}
