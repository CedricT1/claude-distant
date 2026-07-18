//go:build linux

package main

import (
	"os/exec"
	"syscall"
)

// startProcess starts cmd in its own process group so killProcessTree can
// terminate the whole tree (not just the direct child) on timeout/shutdown.
func startProcess(cmd *exec.Cmd) error {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
	return cmd.Start()
}

// killProcessTree forcefully kills cmd's whole process group.
func killProcessTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	pgid, err := syscall.Getpgid(cmd.Process.Pid)
	if err == nil {
		_ = syscall.Kill(-pgid, syscall.SIGKILL)
		return
	}
	_ = cmd.Process.Kill()
}
