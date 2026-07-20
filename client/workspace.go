package main

import (
	"fmt"
	"os"
	"path/filepath"
)

// Workspace is the client's single dedicated scratch directory. Anything
// the client needs to write to disk (temporary scripts, intermediate
// files for command execution, etc.) must live under here so that a single
// Cleanup() call at shutdown — normal exit, signal, or panic — leaves the
// target machine exactly as it found it (residue-free runtime, docs/PLAN.md
// Phase 6).
type Workspace struct {
	dir string
}

// NewWorkspace creates a fresh, private temporary directory under the OS
// default temp location (os.TempDir()). The directory is created with
// owner-only permissions where the OS honors that (0700).
func NewWorkspace() (*Workspace, error) {
	dir, err := os.MkdirTemp("", "claude-distant-*")
	if err != nil {
		return nil, fmt.Errorf("création du workspace temporaire: %w", err)
	}
	// Best-effort: some filesystems/platforms (e.g. Windows/FAT) don't
	// honor Unix permission bits, and that's fine — this is defense in
	// depth, not the primary residue-free guarantee (Cleanup is).
	_ = os.Chmod(dir, 0o700)
	return &Workspace{dir: dir}, nil
}

// Dir returns the workspace's absolute path. Returns "" once Cleanup has
// run (or on a nil receiver).
func (w *Workspace) Dir() string {
	if w == nil {
		return ""
	}
	return w.dir
}

// Path joins name onto the workspace directory, for callers that need to
// create a specific file inside it.
func (w *Workspace) Path(name string) string {
	if w == nil {
		return name
	}
	return filepath.Join(w.dir, name)
}

// Cleanup removes the workspace directory and everything under it. It is
// safe to call multiple times — including once from a normal deferred
// call and again from a panic-recovery path — and safe to call even if the
// directory was already removed by some other means.
func (w *Workspace) Cleanup() error {
	if w == nil || w.dir == "" {
		return nil
	}
	err := os.RemoveAll(w.dir)
	w.dir = ""
	return err
}
