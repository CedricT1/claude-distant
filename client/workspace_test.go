package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// Red-first tests for the residue-free scratch workspace (workspace.go
// does not exist yet): a dedicated temp directory created at startup and
// fully removed at Cleanup(), including when cleanup is triggered by a
// cancelled context (the same path a SIGINT/SIGTERM signal takes via
// signal.NotifyContext in main.go).

func TestNewWorkspace_CreatesDirectory(t *testing.T) {
	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("NewWorkspace() error: %v", err)
	}
	defer ws.Cleanup()

	info, err := os.Stat(ws.Dir())
	if err != nil {
		t.Fatalf("workspace directory does not exist: %v", err)
	}
	if !info.IsDir() {
		t.Errorf("workspace path %q is not a directory", ws.Dir())
	}
}

func TestWorkspace_Cleanup_RemovesDirectoryAndContents(t *testing.T) {
	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("NewWorkspace() error: %v", err)
	}

	// Simulate the client writing a scratch file inside the workspace.
	nested := filepath.Join(ws.Dir(), "sub")
	if err := os.MkdirAll(nested, 0o700); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	if err := os.WriteFile(filepath.Join(nested, "scratch.txt"), []byte("data"), 0o600); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	dir := ws.Dir()
	if err := ws.Cleanup(); err != nil {
		t.Fatalf("Cleanup() error: %v", err)
	}

	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Errorf("workspace directory %q still exists after Cleanup(), err=%v", dir, err)
	}
}

func TestWorkspace_Cleanup_SafeToCallTwice(t *testing.T) {
	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("NewWorkspace() error: %v", err)
	}
	if err := ws.Cleanup(); err != nil {
		t.Fatalf("first Cleanup() error: %v", err)
	}
	if err := ws.Cleanup(); err != nil {
		t.Fatalf("second Cleanup() error: %v", err)
	}
}

func TestWorkspace_Cleanup_RunsOnContextCancellation(t *testing.T) {
	// This models the exact shutdown path main.go takes: an outer
	// signal.NotifyContext is cancelled (Ctrl-C/SIGTERM), runForever
	// returns, and the deferred workspace Cleanup() fires. Here we drive
	// it directly with a cancellable context instead of a real OS signal.
	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("NewWorkspace() error: %v", err)
	}
	dir := ws.Dir()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		<-ctx.Done()
		ws.Cleanup()
		close(done)
	}()

	cancel()
	<-done

	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Errorf("workspace directory %q still exists after signal-triggered cleanup", dir)
	}
}

func TestWorkspace_Path_JoinsUnderDirectory(t *testing.T) {
	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("NewWorkspace() error: %v", err)
	}
	defer ws.Cleanup()

	got := ws.Path("script.sh")
	want := filepath.Join(ws.Dir(), "script.sh")
	if got != want {
		t.Errorf("Path(%q) = %q, want %q", "script.sh", got, want)
	}
}
