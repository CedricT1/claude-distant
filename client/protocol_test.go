package main

import (
	"encoding/json"
	"testing"
)

// These tests pin down the JSON wire format described in docs/PROTOCOL.md
// for every message exchanged over the client<->relay WebSocket channel.
// They are written before protocol.go exists (red), so the constructors
// and types below are the minimal contract protocol.go must satisfy.

func TestRegisterMessage_MarshalsProtocolFields(t *testing.T) {
	msg := NewRegisterMessage("linux", "srv01", "0.1.0", nil, "")

	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	want := map[string]interface{}{
		"type":     "register",
		"os":       "linux",
		"hostname": "srv01",
		"version":  "0.1.0",
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("field %q = %v, want %v", k, got[k], v)
		}
	}
}

// Capability negotiation is a purely additive protocol extension: a client
// that declares nothing must serialize `register` byte-for-byte as before,
// i.e. WITHOUT a "capabilities" key (omitempty). An old relay reading the
// message must not see a new field, and a new relay must record ().
func TestRegisterMessage_OmitsCapabilitiesWhenNone(t *testing.T) {
	for name, caps := range map[string][]string{
		"nil":   nil,
		"empty": {},
	} {
		data, err := json.Marshal(NewRegisterMessage("linux", "srv01", "0.1.0", caps, ""))
		if err != nil {
			t.Fatalf("%s: marshal: %v", name, err)
		}
		var got map[string]interface{}
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatalf("%s: unmarshal: %v", name, err)
		}
		if _, present := got["capabilities"]; present {
			t.Errorf("%s: champ capabilities présent (%v), attendu absent grâce à omitempty — régression de sérialisation", name, got["capabilities"])
		}
	}
}

func TestRegisterMessage_WithCapabilities(t *testing.T) {
	data, err := json.Marshal(NewRegisterMessage("windows", "srv02", "0.2.0", []string{"file_transfer"}, ""))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	caps, ok := got["capabilities"].([]interface{})
	if !ok {
		t.Fatalf("capabilities = %#v, want a JSON array", got["capabilities"])
	}
	if len(caps) != 1 || caps[0] != "file_transfer" {
		t.Errorf("capabilities = %v, want [file_transfer]", caps)
	}
}

// desired_code follows exactly the same additive, backward-compatible
// pattern as capabilities (docs/PROTOCOL.md): an old relay ignores
// the unknown field, and a client with nothing to request must serialize
// register byte-for-byte as before — no "desired_code" key at all.
func TestRegisterMessage_OmitsDesiredCodeWhenEmpty(t *testing.T) {
	data, err := json.Marshal(NewRegisterMessage("linux", "srv01", "0.1.0", nil, ""))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if _, present := got["desired_code"]; present {
		t.Errorf("champ desired_code présent (%v), attendu absent grâce à omitempty — régression de sérialisation", got["desired_code"])
	}
}

func TestRegisterMessage_WithDesiredCode(t *testing.T) {
	data, err := json.Marshal(NewRegisterMessage("linux", "srv01", "0.1.0", []string{"file_transfer"}, "784123678"))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["desired_code"] != "784123678" {
		t.Errorf("desired_code = %v, want 784123678", got["desired_code"])
	}
}

// ClientCapabilities is what main.go advertises; the relay's capability gate
// keys off this exact string.
func TestClientCapabilities_DeclaresFileTransfer(t *testing.T) {
	found := false
	for _, c := range ClientCapabilities {
		if c == "file_transfer" {
			found = true
		}
	}
	if !found {
		t.Errorf("ClientCapabilities = %v, want it to contain \"file_transfer\"", ClientCapabilities)
	}
}

func TestFileChunkMessage_Marshal(t *testing.T) {
	msg := NewFileChunkMessage("r1", 0, "aGVsbG8=")
	if msg.Type != TypeFileChunk {
		t.Errorf("Type = %v, want %v", msg.Type, TypeFileChunk)
	}

	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	want := map[string]interface{}{
		"type":       "file_chunk",
		"request_id": "r1",
		"seq":        float64(0),
		"data":       "aGVsbG8=",
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("field %q = %v, want %v", k, got[k], v)
		}
	}
}

func TestFileChunkMessage_SeqIsIncreasing(t *testing.T) {
	for seq := 0; seq < 3; seq++ {
		msg := NewFileChunkMessage("r1", seq, "AAAA")
		if msg.Seq != seq || msg.RequestID != "r1" {
			t.Errorf("seq %d: unexpected message %+v", seq, msg)
		}
	}
}

func TestHeartbeatMessage_HasTypeOnly(t *testing.T) {
	data, err := json.Marshal(NewHeartbeatMessage())
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["type"] != "heartbeat" {
		t.Errorf("type = %v, want heartbeat", got["type"])
	}
}

func TestStreamMessage_RoundTrip(t *testing.T) {
	msg := NewStreamMessage("r1", StreamStdout, "hello\n")
	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var got StreamMessage
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Type != TypeStream || got.RequestID != "r1" || got.Stream != StreamStdout || got.Data != "hello\n" {
		t.Errorf("round trip mismatch: %+v", got)
	}
}

func TestStreamMessage_StderrKind(t *testing.T) {
	msg := NewStreamMessage("r2", StreamStderr, "oops")
	data, _ := json.Marshal(msg)
	var got map[string]interface{}
	json.Unmarshal(data, &got)
	if got["stream"] != "stderr" {
		t.Errorf("stream = %v, want stderr", got["stream"])
	}
}

func TestResultMessage_NilErrorMarshalsNull(t *testing.T) {
	msg := NewResultMessage("r1", 0, "")
	data, err := json.Marshal(msg)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	json.Unmarshal(data, &got)
	if got["exit_code"] != float64(0) {
		t.Errorf("exit_code = %v, want 0", got["exit_code"])
	}
	if v, ok := got["error"]; !ok || v != nil {
		t.Errorf("error = %v, want explicit null", got["error"])
	}
}

func TestResultMessage_WithError(t *testing.T) {
	msg := NewResultMessage("r1", 126, "refused_by_user")
	data, _ := json.Marshal(msg)
	var got map[string]interface{}
	json.Unmarshal(data, &got)
	if got["error"] != "refused_by_user" {
		t.Errorf("error = %v, want refused_by_user", got["error"])
	}
	if got["exit_code"] != float64(126) {
		t.Errorf("exit_code = %v, want 126", got["exit_code"])
	}
}

// `meta` is optional and additive: every `result` built by the existing
// NewResultMessage must keep serializing exactly as before, without a "meta"
// key, so an old relay/aggregator sees no change at all.
func TestResultMessage_OmitsMetaWhenAbsent(t *testing.T) {
	for _, msg := range []ResultMessage{
		NewResultMessage("r1", 0, ""),
		NewResultMessage("r1", 126, "refused_by_user"),
		NewResultMessageWithMeta("r1", 0, "", nil),
	} {
		data, err := json.Marshal(msg)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		var got map[string]interface{}
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if _, present := got["meta"]; present {
			t.Errorf("champ meta présent (%v) sur %+v, attendu absent grâce à omitempty", got["meta"], msg)
		}
	}
}

func TestResultMessageWithMeta_Marshal(t *testing.T) {
	meta := map[string]any{
		"path":      "/etc/hosts",
		"size":      1234,
		"sha256":    "deadbeef",
		"truncated": false,
	}
	data, err := json.Marshal(NewResultMessageWithMeta("r1", 0, "", meta))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["exit_code"] != float64(0) {
		t.Errorf("exit_code = %v, want 0", got["exit_code"])
	}
	if v, ok := got["error"]; !ok || v != nil {
		t.Errorf("error = %v, want explicit null", got["error"])
	}
	gotMeta, ok := got["meta"].(map[string]interface{})
	if !ok {
		t.Fatalf("meta = %#v, want a JSON object", got["meta"])
	}
	if gotMeta["path"] != "/etc/hosts" || gotMeta["size"] != float64(1234) ||
		gotMeta["sha256"] != "deadbeef" || gotMeta["truncated"] != false {
		t.Errorf("meta = %v, want the four transfer fields verbatim", gotMeta)
	}
}

func TestResultMessageWithMeta_KeepsErrorAndMetaTogether(t *testing.T) {
	data, err := json.Marshal(NewResultMessageWithMeta("r1", 1, "file_not_found", map[string]any{"path": "/nope"}))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["error"] != "file_not_found" {
		t.Errorf("error = %v, want file_not_found", got["error"])
	}
	if gotMeta, ok := got["meta"].(map[string]interface{}); !ok || gotMeta["path"] != "/nope" {
		t.Errorf("meta = %#v, want {path: /nope}", got["meta"])
	}
}

func TestApprovalResponseMessage_Marshal(t *testing.T) {
	msg := NewApprovalResponseMessage("r1", true)
	data, _ := json.Marshal(msg)
	var got map[string]interface{}
	json.Unmarshal(data, &got)
	if got["type"] != "approval_response" || got["request_id"] != "r1" || got["approved"] != true {
		t.Errorf("unexpected fields: %+v", got)
	}
}

func TestEnvelope_ExtractsTypeFromRegisteredMessage(t *testing.T) {
	raw := []byte(`{"type":"registered","session_code":"784123678"}`)
	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if env.Type != TypeRegistered {
		t.Errorf("type = %v, want %v", env.Type, TypeRegistered)
	}

	var msg RegisteredMessage
	if err := json.Unmarshal(raw, &msg); err != nil {
		t.Fatalf("unmarshal registered: %v", err)
	}
	if msg.SessionCode != "784123678" {
		t.Errorf("session_code = %q, want 784123678", msg.SessionCode)
	}
}

func TestEnvelope_ExtractsCommandMessage(t *testing.T) {
	raw := []byte(`{"type":"command","request_id":"r1","tool":"run_shell","params":{"command":"df -h","shell":"auto","timeout":60}}`)
	var msg CommandMessage
	if err := json.Unmarshal(raw, &msg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if msg.Type != TypeCommand || msg.RequestID != "r1" || msg.Tool != "run_shell" {
		t.Errorf("unexpected message: %+v", msg)
	}

	var params RunParams
	if err := json.Unmarshal(msg.Params, &params); err != nil {
		t.Fatalf("unmarshal params: %v", err)
	}
	if params.Command != "df -h" || params.Shell != "auto" || params.Timeout != 60 {
		t.Errorf("unexpected params: %+v", params)
	}
}

func TestReadFileParams_Decode(t *testing.T) {
	raw := []byte(`{"type":"command","request_id":"r1","tool":"read_file","params":{"path":"/etc/hosts","offset":10,"max_bytes":100}}`)
	var msg CommandMessage
	if err := json.Unmarshal(raw, &msg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if msg.Tool != "read_file" {
		t.Fatalf("tool = %q, want read_file", msg.Tool)
	}

	var p ReadFileParams
	if err := json.Unmarshal(msg.Params, &p); err != nil {
		t.Fatalf("unmarshal params: %v", err)
	}
	if p.Path != "/etc/hosts" || p.Offset != 10 || p.MaxBytes != 100 {
		t.Errorf("unexpected params: %+v", p)
	}
}

func TestReadFileParams_OptionalFieldsDefaultToZero(t *testing.T) {
	var p ReadFileParams
	if err := json.Unmarshal([]byte(`{"path":"/etc/hosts"}`), &p); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if p.Offset != 0 || p.MaxBytes != 0 {
		t.Errorf("offset/max_bytes = %d/%d, want 0/0 (absents = valeurs par défaut)", p.Offset, p.MaxBytes)
	}
}

func TestWriteFileParams_Decode(t *testing.T) {
	raw := []byte(`{"path":"/tmp/x","content_base64":"aGVsbG8=","mode":"0600","create_dirs":true,"overwrite":false}`)
	var p WriteFileParams
	if err := json.Unmarshal(raw, &p); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if p.Path != "/tmp/x" || p.ContentBase64 != "aGVsbG8=" || p.Mode != "0600" || !p.CreateDirs {
		t.Errorf("unexpected params: %+v", p)
	}
	if p.Overwrite == nil || *p.Overwrite {
		t.Errorf("overwrite = %v, want an explicit false", p.Overwrite)
	}
}

// Overwrite is a *bool so the client can tell "absent" (default: true) from
// an explicit false sent by the harness.
func TestWriteFileParams_AbsentOverwriteIsNil(t *testing.T) {
	var p WriteFileParams
	if err := json.Unmarshal([]byte(`{"path":"/tmp/x","content_base64":""}`), &p); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if p.Overwrite != nil {
		t.Errorf("overwrite = %v, want nil (absent) to keep the default true", *p.Overwrite)
	}
	if p.CreateDirs {
		t.Error("create_dirs = true, want false by default")
	}
}

func TestHeartbeatAckMessage_Type(t *testing.T) {
	raw := []byte(`{"type":"heartbeat_ack"}`)
	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if env.Type != TypeHeartbeatAck {
		t.Errorf("type = %v, want %v", env.Type, TypeHeartbeatAck)
	}
}
