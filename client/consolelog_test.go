package main

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// syncBuffer is a bytes.Buffer safe to write to from several goroutines —
// which is exactly how a live session writes to consoleOut: the read loop
// prints the session code while command goroutines print journal lines.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func TestConsoleActivityLog_PrintsOneCompactLinePerEvent(t *testing.T) {
	var out bytes.Buffer
	log := NewConsoleActivityLog(&out)

	when := time.Date(2026, 8, 20, 13, 42, 7, 0, time.UTC)
	log.Append(ActivityEvent{Time: when, Kind: ActivityCommand, Detail: "run_shell : uname -a (request r-1)"})
	log.Append(ActivityEvent{Time: when.Add(time.Second), Kind: ActivityResult, Detail: "request r-1: exit=0"})

	lines := strings.Split(strings.TrimRight(out.String(), "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("printed %d lines, want one per event: %q", len(lines), out.String())
	}
	if lines[0] != "13:42:07 [command] run_shell : uname -a (request r-1)" {
		t.Errorf("first line = %q, want the compact journal format", lines[0])
	}
	if !strings.HasPrefix(lines[1], "13:42:08 [result]") {
		t.Errorf("second line = %q, want the result event", lines[1])
	}
}

// The GUI panel and the console journal must show the same text, so an
// operator reading a screenshot of one recognizes the other.
func TestConsoleActivityLog_MatchesTheGUIPanelFormat(t *testing.T) {
	var out bytes.Buffer
	e := ActivityEvent{Time: time.Now(), Kind: ActivityCommand, Detail: "read_file : /etc/hosts (request r-2)"}

	NewConsoleActivityLog(&out).Append(e)

	if got, want := strings.TrimRight(out.String(), "\n"), renderActivityLine(e); got != want {
		t.Errorf("console line = %q, want the shared rendering %q", got, want)
	}
}

// Commands run concurrently (main.go runs each Handle in its own goroutine),
// so the sink is written to from several goroutines at once: no interleaved
// half-lines, and nothing for -race to complain about.
func TestConsoleActivityLog_ConcurrentWritesStayWholeLines(t *testing.T) {
	out := &syncBuffer{}
	log := NewConsoleActivityLog(out)

	const writers = 8
	const perWriter = 25
	var wg sync.WaitGroup
	wg.Add(writers)
	for i := 0; i < writers; i++ {
		go func() {
			defer wg.Done()
			for j := 0; j < perWriter; j++ {
				log.Append(ActivityEvent{Time: time.Now(), Kind: ActivityCommand, Detail: "run_shell : echo concurrent (request r)"})
			}
		}()
	}
	wg.Wait()

	lines := strings.Split(strings.TrimRight(out.String(), "\n"), "\n")
	if len(lines) != writers*perWriter {
		t.Fatalf("printed %d lines, want %d", len(lines), writers*perWriter)
	}
	for i, line := range lines {
		if !strings.HasSuffix(line, "run_shell : echo concurrent (request r)") {
			t.Fatalf("line %d = %q, want a whole, uninterleaved line", i, line)
		}
	}
}

// A nil sink (or one with no writer) must be a no-op rather than a panic:
// same nil-safety contract as ActivityLog's methods.
func TestConsoleActivityLog_NilIsANoOp(t *testing.T) {
	var log *ConsoleActivityLog
	log.Append(ActivityEvent{Time: time.Now(), Kind: ActivityCommand, Detail: "x"})
	NewConsoleActivityLog(nil).Append(ActivityEvent{Time: time.Now(), Kind: ActivityCommand, Detail: "x"})
}

// End-to-end over a real session: a `command` message pushed by a fake relay
// must show up on the console client's own output, with the command text —
// the console counterpart of the GUI's log panel.
func TestRunSession_PrintsHarnessCommandsToConsole(t *testing.T) {
	upgrader := websocket.Upgrader{}
	done := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()

		var reg map[string]any
		if err := c.ReadJSON(&reg); err != nil || reg["type"] != "register" {
			return
		}
		_ = c.WriteJSON(map[string]any{"type": "registered", "session_code": "784123678"})
		_ = c.WriteJSON(map[string]any{
			"type": "command", "request_id": "r-1", "tool": "read_file",
			"params": map[string]any{"path": "/etc/hosts"},
		})

		// Drain until the client reports the command finished, so the test
		// never asserts on a journal still being written.
		for {
			var msg map[string]any
			if err := c.ReadJSON(&msg); err != nil {
				return
			}
			if msg["type"] == "result" {
				close(done)
				return
			}
		}
	}))
	defer srv.Close()

	out := &syncBuffer{}
	restore := consoleOut
	consoleOut = out
	defer func() { consoleOut = restore }()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	cfg := config{
		url: "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws/client",
		// PolicyAuto: the guard-rail must not block this session on a
		// stdin prompt nobody is there to answer.
		token:         NewSecret("t"),
		policy:        PolicyAuto,
		ephemeralCode: true,
	}

	sessionDone := make(chan struct{})
	go func() {
		defer close(sessionDone)
		_ = runSession(ctx, cfg, nil)
	}()

	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatalf("timeout: aucun result reçu; sortie console = %q", out.String())
	}
	cancel()
	<-sessionDone

	printed := out.String()
	if !strings.Contains(printed, "[command] read_file : /etc/hosts") {
		t.Errorf("console output = %q, want the harness command journaled", printed)
	}
	if !strings.Contains(printed, "Code de session : 784 123 678") {
		t.Errorf("console output = %q, want the session code still printed", printed)
	}
}
