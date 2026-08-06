//go:build !gui

package main

import "context"

// runUI is the entry point main() calls once cfg and ws are ready, whichever
// build tag produced this binary. This file (no "gui" tag, the default `go
// build`) is compiled into every non-GUI binary, including the one every
// existing test links against — it must therefore behave as EXACTLY the
// console client did before this file existed. It is a pure pass-through to
// the pre-existing runForever loop (unchanged, still in main.go): no new
// behavior, no new flag, no new output. That byte-for-byte non-regression is
// the entire point of splitting runUI out — see client/gui.go for the
// `-tags gui` build's Fyne window, which implements this same signature.
func runUI(ctx context.Context, cfg config, ws *Workspace) error {
	return runForever(ctx, cfg, ws)
}
