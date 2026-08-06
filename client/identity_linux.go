//go:build linux

package main

import (
	"fmt"
	"os"
	"strings"
)

// platformMachineIDImpl reads Linux's stable machine identifier: the
// systemd-maintained /etc/machine-id first, falling back to the classic
// D-Bus location some minimal/container images populate instead
// (docs/PROTOCOL.md). Both are plain reads: no residue is left
// behind, preserving the client's no-write-on-identify invariant.
func platformMachineIDImpl() (string, error) {
	for _, path := range []string{"/etc/machine-id", "/var/lib/dbus/machine-id"} {
		b, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if id := strings.TrimSpace(string(b)); id != "" {
			return id, nil
		}
	}
	return "", fmt.Errorf("aucun machine-id lisible (/etc/machine-id, /var/lib/dbus/machine-id)")
}
