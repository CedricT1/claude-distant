//go:build windows

package main

import (
	"os/exec"
	"strconv"
	"syscall"
)

// startProcess starts cmd in a new process group so killProcessTree can
// terminate the whole tree via taskkill on timeout/shutdown.
func startProcess(cmd *exec.Cmd) error {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.CreationFlags |= syscall.CREATE_NEW_PROCESS_GROUP
	return cmd.Start()
}

// killProcessTree forcefully kills cmd and all of its descendants.
func killProcessTree(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	killer := exec.Command("taskkill", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F")
	if err := killer.Run(); err != nil {
		_ = cmd.Process.Kill()
	}
}
