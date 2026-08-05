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
	TypeFileChunk        MessageType = "file_chunk"

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

// CapabilityFileTransfer is the capability string the relay's gate keys off
// before dispatching read_file/write_file to this client. A client that does
// not advertise it never receives those tools.
const CapabilityFileTransfer = "file_transfer"

// ClientCapabilities is the optional feature set this build advertises in
// `register`. Declaring nothing (an older client) keeps the relay in its
// pre-existing behaviour, which is what makes the extension backward
// compatible. Treat as read-only.
var ClientCapabilities = []string{CapabilityFileTransfer}

// Envelope is used to sniff the "type" discriminator of an inbound message
// before decoding it into its concrete Go type.
type Envelope struct {
	Type MessageType `json:"type"`
}

// --- Client -> Relay messages ---

// RegisterMessage announces this client to the relay right after connecting.
// Capabilities is omitempty on purpose: a client with nothing to declare must
// serialize exactly as before the capability negotiation existed, so an older
// relay sees no unknown field at all.
type RegisterMessage struct {
	Type         MessageType `json:"type"`
	OS           string      `json:"os"`
	Hostname     string      `json:"hostname"`
	Version      string      `json:"version"`
	Capabilities []string    `json:"capabilities,omitempty"`
}

// NewRegisterMessage builds a `register` message. osName must be "linux" or
// "windows" per the protocol (typically runtime.GOOS). Pass nil capabilities
// to advertise none.
func NewRegisterMessage(osName, hostname, version string, capabilities []string) RegisterMessage {
	return RegisterMessage{
		Type:         TypeRegister,
		OS:           osName,
		Hostname:     hostname,
		Version:      version,
		Capabilities: capabilities,
	}
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

// FileChunkMessage carries one slice of a file being read from this machine.
// Seq starts at 0 and increases by one per chunk so the relay can detect a
// gap or a reordering; Data is standard, padded base64 of the raw bytes.
type FileChunkMessage struct {
	Type      MessageType `json:"type"`
	RequestID string      `json:"request_id"`
	Seq       int         `json:"seq"`
	Data      string      `json:"data"`
}

// NewFileChunkMessage builds a `file_chunk` message for the given request.
func NewFileChunkMessage(requestID string, seq int, data string) FileChunkMessage {
	return FileChunkMessage{Type: TypeFileChunk, RequestID: requestID, Seq: seq, Data: data}
}

// ResultMessage reports the final outcome of a command execution. Meta is an
// optional, tool-specific payload (file transfers report path/size/sha256
// there); omitempty keeps every pre-existing `result` byte-for-byte identical.
type ResultMessage struct {
	Type      MessageType    `json:"type"`
	RequestID string         `json:"request_id"`
	ExitCode  int            `json:"exit_code"`
	Error     *string        `json:"error"`
	Meta      map[string]any `json:"meta,omitempty"`
}

// NewResultMessage builds a `result` message. An empty errMsg marshals the
// `error` field as an explicit JSON null, matching the protocol's `str|null`.
func NewResultMessage(requestID string, exitCode int, errMsg string) ResultMessage {
	return NewResultMessageWithMeta(requestID, exitCode, errMsg, nil)
}

// NewResultMessageWithMeta builds a `result` message carrying the optional
// `meta` object. A nil meta is omitted from the wire format entirely.
func NewResultMessageWithMeta(requestID string, exitCode int, errMsg string, meta map[string]any) ResultMessage {
	var errPtr *string
	if errMsg != "" {
		errPtr = &errMsg
	}
	return ResultMessage{Type: TypeResult, RequestID: requestID, ExitCode: exitCode, Error: errPtr, Meta: meta}
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

// ReadFileParams is the params shape for read_file. Offset and MaxBytes are
// optional: absent means "from the start" and "as much as the transfer cap
// allows" respectively (see filetransfer.go).
type ReadFileParams struct {
	Path     string `json:"path"`
	Offset   int64  `json:"offset"`
	MaxBytes int64  `json:"max_bytes"`
}

// WriteFileParams is the params shape for write_file. Overwrite is a *bool so
// an absent field (default: replace the target) stays distinguishable from an
// explicit false sent by the harness.
type WriteFileParams struct {
	Path          string `json:"path"`
	ContentBase64 string `json:"content_base64"`
	Mode          string `json:"mode"`
	CreateDirs    bool   `json:"create_dirs"`
	Overwrite     *bool  `json:"overwrite"`
}
