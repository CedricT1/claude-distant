package main

import (
	"os"
	"runtime"
)

// SystemInfo is the payload returned by the system_info tool
// (docs/PROTOCOL.md: "OS, uptime, RAM, CPU").
type SystemInfo struct {
	OS         string `json:"os"`
	Hostname   string `json:"hostname"`
	UptimeSecs int64  `json:"uptime_seconds"`
	CPUCount   int    `json:"cpu_count"`
	MemTotalMB uint64 `json:"mem_total_mb"`
	MemAvailMB uint64 `json:"mem_available_mb"`
}

// getUptimeSeconds and getMemoryMB are implemented per-OS
// (sysinfo_linux.go, sysinfo_windows.go, sysinfo_other.go) using only the
// standard library / golang.org/x/sys — no heavy system-info dependency.
var (
	getUptimeSeconds = platformGetUptimeSeconds
	getMemoryMB      = platformGetMemoryMB
)

// GetSystemInfo collects OS, hostname, uptime, CPU count and memory figures.
func GetSystemInfo() (SystemInfo, error) {
	hostname, err := os.Hostname()
	if err != nil {
		hostname = "unknown"
	}
	info := SystemInfo{
		OS:       runtime.GOOS,
		Hostname: hostname,
		CPUCount: runtime.NumCPU(),
	}
	if uptime, err := getUptimeSeconds(); err == nil {
		info.UptimeSecs = uptime
	}
	if total, avail, err := getMemoryMB(); err == nil {
		info.MemTotalMB = total
		info.MemAvailMB = avail
	}
	return info, nil
}
