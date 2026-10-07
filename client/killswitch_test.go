package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// killSwitchRelay is a fake relay that accepts a `register`, replies with a
// session code, then ends the session the way `terminate` asks it to. It
// counts the connection attempts so the tests can tell "stopped for good"
// from "reconnected after a backoff".
func killSwitchRelay(t *testing.T, attempts *int32, terminate func(c *websocket.Conn)) *httptest.Server {
	t.Helper()
	upgrader := websocket.Upgrader{}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(attempts, 1)
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		var reg struct {
			Type string `json:"type"`
		}
		if err := c.ReadJSON(&reg); err != nil || reg.Type != "register" {
			return
		}
		_ = c.WriteJSON(map[string]any{"type": "registered", "session_code": "784123678"})
		terminate(c)
		// Drain until the client goes away, so the close handshake completes.
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
}

func killSwitchConfig(srv *httptest.Server) config {
	return config{
		url:           "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws/client",
		token:         NewSecret("test-token"),
		policy:        PolicyAuto,
		ephemeralCode: true,
	}
}

// runForeverResult runs runForever in the background and returns its error
// through a channel, so a test can bound how long it waits for it.
func runForeverResult(ctx context.Context, cfg config, ws *Workspace) <-chan error {
	done := make(chan error, 1)
	go func() { done <- runForever(ctx, cfg, ws) }()
	return done
}

// TestRunForever_StopsOnSessionTerminatedMessage pins the kill-switch
// contract: once the relay says `session_terminated`, the client exits its
// reconnect loop for good instead of coming back a second later with the
// same shared token and the same stable session code — which would reduce
// `terminate_session` (docs/SECURITY.md §6) to a brief hiccup.
func TestRunForever_StopsOnSessionTerminatedMessage(t *testing.T) {
	var attempts int32
	srv := killSwitchRelay(t, &attempts, func(c *websocket.Conn) {
		_ = c.WriteJSON(map[string]any{"type": "session_terminated"})
		_ = c.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(CloseCodeSessionTerminated, "terminated"))
	})
	defer srv.Close()

	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("workspace: %v", err)
	}
	defer ws.Cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	select {
	case err := <-runForeverResult(ctx, killSwitchConfig(srv), ws):
		if !errors.Is(err, errSessionTerminated) {
			t.Fatalf("runForever a retourné %v, attendu errSessionTerminated", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("runForever n'est pas sorti après session_terminated : le client s'est reconnecté")
	}
	// Give any (wrong) reconnect attempt ample time to show up: the first
	// backoff is in [0.5s, 1s).
	time.Sleep(1500 * time.Millisecond)
	if n := atomic.LoadInt32(&attempts); n != 1 {
		t.Fatalf("%d connexions au relay après le kill-switch, attendu exactement 1", n)
	}
}

// TestRunForever_StopsOnCloseCode4402Alone covers the case where only the
// close frame gets through (no `session_terminated` message before it):
// the 4402 close code must be enough to stop the client.
func TestRunForever_StopsOnCloseCode4402Alone(t *testing.T) {
	var attempts int32
	srv := killSwitchRelay(t, &attempts, func(c *websocket.Conn) {
		_ = c.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(CloseCodeSessionTerminated, "terminated"))
	})
	defer srv.Close()

	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("workspace: %v", err)
	}
	defer ws.Cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	select {
	case err := <-runForeverResult(ctx, killSwitchConfig(srv), ws):
		if !errors.Is(err, errSessionTerminated) {
			t.Fatalf("runForever a retourné %v, attendu errSessionTerminated", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("runForever n'est pas sorti après la fermeture 4402")
	}
	time.Sleep(1500 * time.Millisecond)
	if n := atomic.LoadInt32(&attempts); n != 1 {
		t.Fatalf("%d connexions au relay après le kill-switch, attendu exactement 1", n)
	}
}

// TestRunForever_StillReconnectsAfterOrdinaryClose is the non-regression
// for the fix above: an ordinary close (normal closure, network blip) must
// keep triggering the reconnect-with-backoff loop exactly as before.
func TestRunForever_StillReconnectsAfterOrdinaryClose(t *testing.T) {
	var attempts int32
	srv := killSwitchRelay(t, &attempts, func(c *websocket.Conn) {
		_ = c.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, "bye"))
	})
	defer srv.Close()

	ws, err := NewWorkspace()
	if err != nil {
		t.Fatalf("workspace: %v", err)
	}
	defer ws.Cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()

	err = <-runForeverResult(ctx, killSwitchConfig(srv), ws)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("runForever a retourné %v, attendu l'expiration du contexte", err)
	}
	if n := atomic.LoadInt32(&attempts); n < 2 {
		t.Fatalf("%d connexion(s) seulement : la reconnexion après une fermeture ordinaire ne fonctionne plus", n)
	}
}
