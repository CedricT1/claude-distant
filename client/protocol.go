package main

import "encoding/json"

// MessageType is the "type" discriminator present on every message
// exchanged over the client<->relay WebSocket channel (docs/PROTOCOL.md §1).
type MessageType string

const (
	// Client -> Relay
	TypeRegister         MessageType = "register"
	TypeHeartbeat        MessageType = "heartbeat"
	TypeStream           MessageType = "stream"
	TypeResult           MessageType = "result"
	TypeApprovalResponse MessageType = "approval_response"

	// Relay -> Client
	TypeRegistered   MessageType = "registered"
	TypeCommand      MessageType = "command"
	TypeHeartbeatAck MessageType = "heartbeat_ack"
)

// StreamKind identifies which output stream a `stream` message carries.
type StreamKind string

const (
	StreamStdout StreamKind = "stdout"
	StreamStderr StreamKind = "stderr"
)

// Envelope is used to sniff the "type" discriminator of an inbound message
// before decoding it into its concrete Go type.
type Envelope struct {
	Type MessageType `json:"type"`
}

// --- Client -> Relay messages ---

// RegisterMessage announces this client to the relay right after connecting.
type RegisterMessage struct {
	Type     MessageType `json:"type"`
	OS       string      `json:"os"`
	Hostname string      `json:"hostname"`
	Version  string      `json:"version"`
}

// NewRegisterMessage builds a `register` message. osName must be "linux" or
// "windows" per the protocol (typically runtime.GOOS).
func NewRegisterMessage(osName, hostname, version string) RegisterMessage {
	return RegisterMessage{Type: TypeRegister, OS: osName, Hostname: hostname, Version: version}
}

// HeartbeatMessage keeps the session alive.
type HeartbeatMessage struct {
	Type MessageType `json:"type"`
}

// NewHeartbeatMessage builds a `heartbeat` message.
func NewHeartbeatMessage() HeartbeatMessage {
	return HeartbeatMessage{Type: TypeHeartbeat}
}

// StreamMessage carries a partial chunk of stdout/stderr for a running command.
type StreamMessage struct {
	Type      MessageType `json:"type"`
	RequestID string      `json:"request_id"`
	Stream    StreamKind  `json:"stream"`
	Data      string      `json:"data"`
}

// NewStreamMessage builds a `stream` message for the given request.
func NewStreamMessage(requestID string, kind StreamKind, data string) StreamMessage {
	return StreamMessage{Type: TypeStream, RequestID: requestID, Stream: kind, Data: data}
}

// ResultMessage reports the final outcome of a command execution.
type ResultMessage struct {
	Type      MessageType `json:"type"`
	RequestID string      `json:"request_id"`
	ExitCode  int         `json:"exit_code"`
	Error     *string     `json:"error"`
}

// NewResultMessage builds a `result` message. An empty errMsg marshals the
// `error` field as an explicit JSON null, matching the protocol's `str|null`.
func NewResultMessage(requestID string, exitCode int, errMsg string) ResultMessage {
	var errPtr *string
	if errMsg != "" {
		errPtr = &errMsg
	}
	return ResultMessage{Type: TypeResult, RequestID: requestID, ExitCode: exitCode, Error: errPtr}
}

// ApprovalResponseMessage reports the local guard-rail decision for a command
// back to the relay (audit trail), when the confirm/deny policy engaged.
type ApprovalResponseMessage struct {
	Type      MessageType `json:"type"`
	RequestID string      `json:"request_id"`
	Approved  bool        `json:"approved"`
}

// NewApprovalResponseMessage builds an `approval_response` message.
func NewApprovalResponseMessage(requestID string, approved bool) ApprovalResponseMessage {
	return ApprovalResponseMessage{Type: TypeApprovalResponse, RequestID: requestID, Approved: approved}
}

// --- Relay -> Client messages ---

// RegisteredMessage carries the 9-digit session code assigned by the relay.
type RegisteredMessage struct {
	Type        MessageType `json:"type"`
	SessionCode string      `json:"session_code"`
}

// CommandMessage asks the client to run a tool. Params is kept as raw JSON
// so each tool can decode only the fields it understands (see RunParams).
type CommandMessage struct {
	Type      MessageType     `json:"type"`
	RequestID string          `json:"request_id"`
	Tool      string          `json:"tool"`
	Params    json.RawMessage `json:"params"`
}

// HeartbeatAckMessage acknowledges a client heartbeat.
type HeartbeatAckMessage struct {
	Type MessageType `json:"type"`
}

// --- Tool params ---

// RunParams is the params shape for both run_shell and run_command. Shell is
// ignored by run_command (which never spawns a shell).
type RunParams struct {
	Command string `json:"command"`
	Shell   string `json:"shell"`
	Timeout int    `json:"timeout"`
}
