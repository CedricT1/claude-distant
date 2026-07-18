//go:build windows

package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

// Bound directly to kernel32.dll via the standard library's syscall
// package: no extra system-info dependency is needed for Windows either.
var (
	modKernel32              = syscall.NewLazyDLL("kernel32.dll")
	procGetTickCount64       = modKernel32.NewProc("GetTickCount64")
	procGlobalMemoryStatusEx = modKernel32.NewProc("GlobalMemoryStatusEx")
)

// memoryStatusEx mirrors the Win32 MEMORYSTATUSEX struct.
type memoryStatusEx struct {
	cbSize                  uint32
	dwMemoryLoad            uint32
	ullTotalPhys            uint64
	ullAvailPhys            uint64
	ullTotalPageFile        uint64
	ullAvailPageFile        uint64
	ullTotalVirtual         uint64
	ullAvailVirtual         uint64
	ullAvailExtendedVirtual uint64
}

func platformGetUptimeSeconds() (int64, error) {
	r0, _, _ := procGetTickCount64.Call()
	return int64(r0 / 1000), nil
}

func platformGetMemoryMB() (totalMB, availMB uint64, err error) {
	var status memoryStatusEx
	status.cbSize = uint32(unsafe.Sizeof(status))
	r0, _, callErr := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&status)))
	if r0 == 0 {
		return 0, 0, fmt.Errorf("GlobalMemoryStatusEx: %w", callErr)
	}
	const mb = 1024 * 1024
	return status.ullTotalPhys / mb, status.ullAvailPhys / mb, nil
}
