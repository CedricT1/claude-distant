package main

// SecretBytes holds sensitive data (e.g. the pre-shared client bearer
// token) as a mutable byte slice instead of an immutable Go string, so it
// can be explicitly overwritten ("zeroized") once no longer needed instead
// of relying on the garbage collector to eventually reclaim — and never
// scrub — the original plaintext. Part of the "sans trace" runtime
// (docs/PLAN.md Phase 6): best-effort erasure of secrets in memory at
// shutdown.
type SecretBytes struct {
	data []byte
}

// NewSecret copies s into a fresh, independent SecretBytes.
func NewSecret(s string) *SecretBytes {
	b := make([]byte, len(s))
	copy(b, s)
	return &SecretBytes{data: b}
}

// String returns the secret's current value. Each call allocates a new Go
// string backed by the runtime's ordinary (unzeroizable) string storage,
// so callers should fetch it right before use (e.g. immediately before
// dialing the relay) rather than retaining the returned value.
func (s *SecretBytes) String() string {
	if s == nil {
		return ""
	}
	return string(s.data)
}

// Zero overwrites the secret's backing array with zero bytes. Best-effort:
// it scrubs this buffer, but cannot reach back into any string copies
// already produced by earlier String() calls, nor account for copies the
// Go runtime/compiler may have made that this type doesn't track. Safe to
// call on a nil receiver and safe to call more than once.
func (s *SecretBytes) Zero() {
	if s == nil {
		return
	}
	ZeroBytes(s.data)
	s.data = nil
}

// ZeroBytes overwrites b with zeros in place. Exposed standalone for
// zeroizing plain []byte buffers that don't need the SecretBytes wrapper.
func ZeroBytes(b []byte) {
	for i := range b {
		b[i] = 0
	}
}
