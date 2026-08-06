//go:build windows

package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

// platformMachineIDImpl reads Windows' stable per-installation identifier,
// MachineGuid, from the registry at SOFTWARE\Microsoft\Cryptography — the
// same value Windows itself uses to identify this OS install
// (docs/PROTOCOL.md). It uses the standard library's low-level
// syscall bindings directly (no extra dependency), the same style already
// used for GlobalMemoryStatusEx in sysinfo_windows.go. Read-only: no
// residue is left behind.
func platformMachineIDImpl() (string, error) {
	var key syscall.Handle
	subkeyPath, err := syscall.UTF16PtrFromString(`SOFTWARE\Microsoft\Cryptography`)
	if err != nil {
		return "", err
	}
	// KEY_WOW64_64KEY forces a 32-bit process to read the 64-bit view of
	// the registry, where MachineGuid actually lives; harmless on a native
	// 64-bit process.
	if err := syscall.RegOpenKeyEx(syscall.HKEY_LOCAL_MACHINE, subkeyPath, 0, syscall.KEY_READ|syscall.KEY_WOW64_64KEY, &key); err != nil {
		return "", fmt.Errorf("ouverture de la clé de registre Cryptography: %w", err)
	}
	defer syscall.RegCloseKey(key)

	valueName, err := syscall.UTF16PtrFromString("MachineGuid")
	if err != nil {
		return "", err
	}

	var valueType uint32
	var bufLen uint32
	if err := syscall.RegQueryValueEx(key, valueName, nil, &valueType, nil, &bufLen); err != nil {
		return "", fmt.Errorf("lecture de MachineGuid (taille): %w", err)
	}
	if bufLen == 0 {
		return "", fmt.Errorf("MachineGuid vide")
	}

	buf := make([]byte, bufLen)
	if err := syscall.RegQueryValueEx(key, valueName, nil, &valueType, &buf[0], &bufLen); err != nil {
		return "", fmt.Errorf("lecture de MachineGuid: %w", err)
	}

	// REG_SZ values are stored as NUL-terminated UTF-16; reinterpret the
	// byte buffer as a []uint16 before decoding.
	u16 := make([]uint16, bufLen/2)
	for i := range u16 {
		u16[i] = *(*uint16)(unsafe.Pointer(&buf[i*2]))
	}
	guid := syscall.UTF16ToString(u16)
	if guid == "" {
		return "", fmt.Errorf("MachineGuid vide")
	}
	return guid, nil
}
