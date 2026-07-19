package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// TestReadLoopStaysResponsiveDuringCommand is a regression test for the bug
// where the client executed commands synchronously inside its WebSocket read
// loop: while a long command ran, the loop stopped reading (no heartbeat_ack,
// read deadline eventually expiring) and the connection dropped.
//
// The fix runs each command in its own goroutine, so the read loop keeps
// processing inbound messages during execution. We prove that here by having
// the fake relay send a SLOW command immediately followed by a FAST one: only
// a responsive (non-blocked) read loop can pick up and complete the fast
// command before the slow one finishes. With the old synchronous code the
// second command would not even be read until the first returned, so its
// result would always arrive last.
func TestReadLoopStaysResponsiveDuringCommand(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash indisponible sur cet hôte")
	}

	type incoming struct {
		Type      string `json:"type"`
		OS        string `json:"os"`
		RequestID string `json:"request_id"`
		ExitCode  int    `json:"exit_code"`
	}

	resultOrder := make(chan string, 2)
	upgrader := websocket.Upgrader{}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()

		// 1. Expect register, reply with a session code.
		var reg incoming
		if err := c.ReadJSON(&reg); err != nil || reg.Type != "register" {
			return
		}
		_ = c.WriteJSON(map[string]any{"type": "registered", "session_code": "784123678"})

		// 2. Send a slow command, then immediately a fast one.
		_ = c.WriteJSON(map[string]any{
			"type": "command", "request_id": "slow", "tool": "run_shell",
			"params": map[string]any{"command": "sleep 1; echo slow-done", "shell": "auto"},
		})
		_ = c.WriteJSON(map[string]any{
			"type": "command", "request_id": "fast", "tool": "run_shell",
			"params": map[string]any{"command": "echo fast-done", "shell": "auto"},
		})

		// 3. Record the order in which the two `result` messages arrive.
		for {
			var msg incoming
			if err := c.ReadJSON(&msg); err != nil {
				return
			}
			if msg.Type == "result" {
				select {
				case resultOrder <- msg.RequestID:
				default:
				}
			}
		}
	}))
	defer srv.Close()

	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("workspace: %v", err)
	}
	defer ws.Cleanup()

	cfg := config{
		url:    "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws/client",
		token:  NewSecret("test-token"),
		policy: PolicyAuto,
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	go func() { _ = runSession(ctx, cfg, ws) }()

	first := recvOrFail(t, resultOrder)
	second := recvOrFail(t, resultOrder)

	if first != "fast" {
		t.Fatalf("la boucle de lecture était bloquée : le premier result reçu est %q (attendu \"fast\"), ordre = [%s, %s]", first, first, second)
	}
	if second != "slow" {
		t.Fatalf("second result inattendu : %q (attendu \"slow\")", second)
	}
}

func recvOrFail(t *testing.T, ch <-chan string) string {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(8 * time.Second):
		t.Fatal("timeout en attente d'un message result du client")
		return ""
	}
}
