package main

import (
	"errors"
	"testing"
)

// Red-first tests for the stable-address derivation (identity.go does not
// exist yet). DeriveSessionCode's expected values
// below were computed independently (HMAC-SHA256, first 8 digest bytes
// big-endian, modulo 1e9) outside of this implementation, so a bug in the
// algorithm itself — not just a typo mirrored on both sides — would be
// caught.

func TestDeriveSessionCode_KnownVectors(t *testing.T) {
	cases := []struct {
		secret, machineID, want string
	}{
		{"secret1", "machine-abc-123", "648443614"},
		{"deployment-salt", "9f2c1e6a4b7d8035c1e2a3f4b5c6d7e8", "386742645"},
		{"", "some-machine-id", "111711239"},
	}
	for _, c := range cases {
		got := DeriveSessionCode(c.secret, c.machineID)
		if got != c.want {
			t.Errorf("DeriveSessionCode(%q, %q) = %q, want %q", c.secret, c.machineID, got, c.want)
		}
	}
}

func TestDeriveSessionCode_AlwaysNineDigits(t *testing.T) {
	inputs := []struct{ secret, machineID string }{
		{"a", "b"},
		{"", ""},
		{"long-secret-value-xyz", "another-machine-id-here"},
		{"unicode-é-ç-secret", "unicode-機械-id"},
	}
	for _, in := range inputs {
		code := DeriveSessionCode(in.secret, in.machineID)
		if len(code) != 9 {
			t.Errorf("DeriveSessionCode(%q, %q) = %q, want length 9", in.secret, in.machineID, code)
		}
		for _, r := range code {
			if r < '0' || r > '9' {
				t.Errorf("DeriveSessionCode(%q, %q) = %q, want digits only", in.secret, in.machineID, code)
				break
			}
		}
	}
}

func TestDeriveSessionCode_Deterministic(t *testing.T) {
	a := DeriveSessionCode("s", "m")
	b := DeriveSessionCode("s", "m")
	if a != b {
		t.Errorf("DeriveSessionCode is not deterministic: %q != %q", a, b)
	}
}

func TestDeriveSessionCode_DifferentSecretsDiffer(t *testing.T) {
	a := DeriveSessionCode("secret-a", "same-machine")
	b := DeriveSessionCode("secret-b", "same-machine")
	if a == b {
		t.Errorf("DeriveSessionCode gave the same code %q for two different secrets", a)
	}
}

func TestDeriveSessionCode_DifferentMachineIDsDiffer(t *testing.T) {
	a := DeriveSessionCode("same-secret", "machine-a")
	b := DeriveSessionCode("same-secret", "machine-b")
	if a == b {
		t.Errorf("DeriveSessionCode gave the same code %q for two different machine IDs", a)
	}
}

// --- MachineID: platform read with a hostname fallback ---
//
// platformMachineID and hostnameFn are package-level vars (like sysinfo.go's
// getUptimeSeconds/getMemoryMB) precisely so this fallback chain can be
// unit-tested without depending on the real machine's /etc/machine-id or
// Windows registry.

func TestMachineID_UsesPlatformValueWhenAvailable(t *testing.T) {
	restorePlatform := platformMachineID
	defer func() { platformMachineID = restorePlatform }()
	platformMachineID = func() (string, error) { return "platform-id-123", nil }

	id, err := MachineID()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if id != "platform-id-123" {
		t.Errorf("MachineID = %q, want the platform-supplied value", id)
	}
}

func TestMachineID_FallsBackToHostnameWhenPlatformFails(t *testing.T) {
	restorePlatform, restoreHostname := platformMachineID, hostnameFn
	defer func() { platformMachineID, hostnameFn = restorePlatform, restoreHostname }()
	platformMachineID = func() (string, error) { return "", errors.New("no machine-id readable") }
	hostnameFn = func() (string, error) { return "fallback-host", nil }

	id, err := MachineID()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if id != "fallback-host" {
		t.Errorf("MachineID = %q, want the hostname fallback", id)
	}
}

func TestMachineID_ErrorsWhenBothPlatformAndHostnameFail(t *testing.T) {
	restorePlatform, restoreHostname := platformMachineID, hostnameFn
	defer func() { platformMachineID, hostnameFn = restorePlatform, restoreHostname }()
	platformMachineID = func() (string, error) { return "", errors.New("no machine-id readable") }
	hostnameFn = func() (string, error) { return "", errors.New("no hostname either") }

	if _, err := MachineID(); err == nil {
		t.Error("expected an error when neither the platform ID nor the hostname is available")
	}
}

func TestDefaultIdentitySalt_IsNonEmpty(t *testing.T) {
	// The stable-address feature must work out of the box (a deliberate
	// project default, docs/PROTOCOL.md), so this constant must
	// never be blank.
	if defaultIdentitySalt == "" {
		t.Error("defaultIdentitySalt is empty, want a non-empty project-wide default salt")
	}
}
