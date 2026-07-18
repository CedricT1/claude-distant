package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	handshakeTimeout = 15 * time.Second
	writeTimeout     = 10 * time.Second
	// readTimeout must comfortably exceed heartbeatInterval (main.go) so a
	// missed heartbeat_ack or two doesn't trip a false disconnect.
	readTimeout = 90 * time.Second
)

// Conn wraps a WebSocket connection to the relay's /ws/client endpoint and
// provides thread-safe JSON message I/O for the protocol described in
// docs/PROTOCOL.md.
type Conn struct {
	ws *websocket.Conn
	mu sync.Mutex
}

// DialRelay opens the outbound connection and authenticates with the
// pre-shared client token via the Authorization: Bearer header.
func DialRelay(ctx context.Context, url, token string, insecureSkipVerify bool) (*Conn, error) {
	dialer := websocket.Dialer{
		HandshakeTimeout: handshakeTimeout,
		Proxy:            http.ProxyFromEnvironment,
	}
	if insecureSkipVerify {
		dialer.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // opt-in dev flag only
	}

	header := http.Header{}
	header.Set("Authorization", "Bearer "+token)

	ws, resp, err := dialer.DialContext(ctx, url, header)
	if err != nil {
		if resp != nil {
			return nil, fmt.Errorf("connexion au relay: %w (statut http %s)", err, resp.Status)
		}
		return nil, fmt.Errorf("connexion au relay: %w", err)
	}

	c := &Conn{ws: ws}
	if err := c.ws.SetReadDeadline(time.Now().Add(readTimeout)); err != nil {
		_ = ws.Close()
		return nil, err
	}
	return c, nil
}

// WriteJSON marshals v and sends it as a single text frame. Safe for
// concurrent use.
func (c *Conn) WriteJSON(v interface{}) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.ws.SetWriteDeadline(time.Now().Add(writeTimeout)); err != nil {
		return err
	}
	return c.ws.WriteJSON(v)
}

// ReadEnvelope blocks until the next message arrives, returning its "type"
// discriminator together with the raw bytes so the caller can decode into
// the concrete message type.
func (c *Conn) ReadEnvelope() (MessageType, []byte, error) {
	_, data, err := c.ws.ReadMessage()
	if err != nil {
		return "", nil, err
	}
	if err := c.ws.SetReadDeadline(time.Now().Add(readTimeout)); err != nil {
		return "", nil, err
	}
	var env Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return "", nil, fmt.Errorf("décodage message: %w", err)
	}
	return env.Type, data, nil
}

// Close sends a normal WebSocket close frame and releases the connection.
// Safe to call more than once.
func (c *Conn) Close() error {
	c.mu.Lock()
	_ = c.ws.SetWriteDeadline(time.Now().Add(writeTimeout))
	_ = c.ws.WriteMessage(websocket.CloseMessage,
		websocket.FormatCloseMessage(websocket.CloseNormalClosure, "shutting down"))
	c.mu.Unlock()
	return c.ws.Close()
}
