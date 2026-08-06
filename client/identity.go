package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"os"
)

// defaultIdentitySalt derives the stable session code when no
// IDENTITY_SECRET was compiled into this binary (buildconfig.go's
// buildIdentitySecret). Baking in a project-wide default is what makes the
// stable-address feature (docs/PROTOCOL.md) work out of the box
// without requiring a custom build — a deliberate ergonomics choice.
//
// The cost, to document without detour: anyone who knows a target
// machine's MachineID (itself not a strong secret — e.g. Linux's
// /etc/machine-id is world-readable by design) and this public salt can
// predict its session code. Compiling a deployment-specific
// IDENTITY_SECRET (client/Makefile's IDENTITY_SECRET variable) is strongly
// recommended for anything beyond casual/local use (docs/SECURITY.md).
const defaultIdentitySalt = "claude-distant-default-identity-salt-v1"

// platformMachineID reads the OS-specific stable machine identifier
// (identity_linux.go, identity_windows.go). A package-level var — like
// sysinfo.go's getUptimeSeconds/getMemoryMB — so MachineID's fallback
// chain is unit-testable without touching the real machine.
var platformMachineID = platformMachineIDImpl

// hostnameFn is os.Hostname, boxed as a var so MachineID's fallback path is
// unit-testable without depending on the real process's hostname.
var hostnameFn = os.Hostname

// MachineID returns a stable identifier for the current machine, read
// WITHOUT ever writing to disk — deriving the stable address must leave no
// trace, same as everything else in the residue-free runtime (docs/PLAN.md
// Phase 6). It first tries the platform-specific identifier
// (platformMachineID); if that fails or is empty, it falls back to the
// hostname, and only returns an error if neither is available.
func MachineID() (string, error) {
	if id, err := platformMachineID(); err == nil && id != "" {
		return id, nil
	}
	host, err := hostnameFn()
	if err != nil || host == "" {
		return "", fmt.Errorf("aucun identifiant machine disponible (ni identifiant système, ni hostname)")
	}
	return host, nil
}

// DeriveSessionCode deterministically computes the 9-digit stable session
// code for one machine: HMAC-SHA256(secret, machineID), the first 8 digest
// bytes read as a big-endian uint64, taken modulo 1e9 and zero-padded to 9
// digits. Pure and side-effect free, so it is fully unit-testable; secret
// should be deployment-specific (see defaultIdentitySalt) for the result to
// be unpredictable to anyone who doesn't hold it.
func DeriveSessionCode(secret, machineID string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(machineID))
	digest := mac.Sum(nil)
	n := binary.BigEndian.Uint64(digest[:8])
	code := n % 1_000_000_000
	return fmt.Sprintf("%09d", code)
}
