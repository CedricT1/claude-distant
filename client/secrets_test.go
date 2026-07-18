package main

import "testing"

// Red-first tests for best-effort in-memory secret zeroization (secrets.go
// does not exist yet). The client holds the bearer token as a []byte
// wrapped in SecretBytes instead of an immutable string precisely so it can
// be overwritten (zeroized) once no longer needed, at shutdown.

func TestNewSecret_StringReturnsOriginalValue(t *testing.T) {
	s := NewSecret("super-secret-token")
	if got := s.String(); got != "super-secret-token" {
		t.Errorf("String() = %q, want %q", got, "super-secret-token")
	}
}

func TestSecretBytes_ZeroOverwritesUnderlyingData(t *testing.T) {
	s := NewSecret("super-secret-token")
	s.Zero()
	if got := s.String(); got != "" {
		t.Errorf("String() after Zero() = %q, want empty", got)
	}
}

func TestSecretBytes_ZeroOnNilIsNoop(t *testing.T) {
	var s *SecretBytes
	s.Zero() // must not panic
	if got := s.String(); got != "" {
		t.Errorf("String() on nil = %q, want empty", got)
	}
}

func TestSecretBytes_MutatingReturnedStringDoesNotAffectSecret(t *testing.T) {
	// Guards the invariant that NewSecret copies its input: mutating the
	// original byte slice passed in must not be reflected in the secret.
	original := []byte("abc-token")
	s := NewSecret(string(original))
	original[0] = 'X'
	if got := s.String(); got != "abc-token" {
		t.Errorf("String() = %q, want %q (independent copy)", got, "abc-token")
	}
}

func TestZeroBytes_OverwritesSliceInPlace(t *testing.T) {
	b := []byte("secret-data")
	ZeroBytes(b)
	for i, c := range b {
		if c != 0 {
			t.Fatalf("byte %d = %d, want 0 (b=%v)", i, c, b)
		}
	}
}
