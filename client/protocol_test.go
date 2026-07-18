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
	msg := NewRegisterMessage("linux", "srv01", "0.1.0")

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
