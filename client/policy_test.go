package main

import (
	"bufio"
	"strings"
	"testing"
)

// Red-first tests for the local guard-rail: policy parsing and the
// destructive-command classifier (policy.go does not exist yet).

func TestParsePolicy_ValidValues(t *testing.T) {
	cases := map[string]Policy{
		"auto":    PolicyAuto,
		"confirm": PolicyConfirm,
		"deny":    PolicyDeny,
		"AUTO":    PolicyAuto,
		" Deny ":  PolicyDeny,
	}
	for input, want := range cases {
		got, err := ParsePolicy(input)
		if err != nil {
			t.Errorf("ParsePolicy(%q) unexpected error: %v", input, err)
			continue
		}
		if got != want {
			t.Errorf("ParsePolicy(%q) = %v, want %v", input, got, want)
		}
	}
}

func TestParsePolicy_InvalidValue(t *testing.T) {
	if _, err := ParsePolicy("yolo"); err == nil {
		t.Error("expected error for invalid policy, got nil")
	}
}

func TestIsDestructive_FlagsKnownDangerousCommands(t *testing.T) {
	dangerous := []string{
		"rm -rf /var/lib/data",
		"rm -fr /tmp/x",
		"sudo rm -rf --no-preserve-root /",
		"mkfs.ext4 /dev/sda1",
		"dd if=/dev/zero of=/dev/sda bs=1M",
		"shutdown -h now",
		"reboot",
		"Remove-Item -Recurse -Force C:\\Users\\test",
		"diskpart",
		"format C: /y",
		"wipefs -a /dev/sdb",
		": () { :|:& };:",
	}
	for _, cmd := range dangerous {
		if !IsDestructive(cmd) {
			t.Errorf("IsDestructive(%q) = false, want true", cmd)
		}
	}
}

func TestIsDestructive_LeavesSafeCommandsAlone(t *testing.T) {
	safe := []string{
		"df -h",
		"ls -la /home",
		"echo hello world",
		"systemctl status nginx",
		"Get-Process",
		"cat /etc/os-release",
		"rm file.txt",
	}
	for _, cmd := range safe {
		if IsDestructive(cmd) {
			t.Errorf("IsDestructive(%q) = true, want false", cmd)
		}
	}
}

func TestPromptConfirm_Answers(t *testing.T) {
	cases := map[string]struct {
		wantApproved bool
		wantAlways   bool
	}{
		"o\n":              {true, false},
		"oui\n":            {true, false},
		"y\n":              {true, false},
		"Yes\n":            {true, false},
		"t\n":              {true, true},
		"toujours\n":       {true, true},
		"ALWAYS\n":         {true, true},
		"n\n":              {false, false},
		"\n":               {false, false},
		"n'importe quoi\n": {false, false},
	}
	for input, want := range cases {
		approved, always := PromptConfirm(bufio.NewReader(strings.NewReader(input)), "rm -rf /tmp/x")
		if approved != want.wantApproved || always != want.wantAlways {
			t.Errorf("PromptConfirm(%q) = (approved=%v, always=%v), want (%v, %v)",
				input, approved, always, want.wantApproved, want.wantAlways)
		}
	}
}

func TestPromptConfirm_EOFDenies(t *testing.T) {
	approved, always := PromptConfirm(bufio.NewReader(strings.NewReader("")), "rm -rf /tmp/x")
	if approved || always {
		t.Errorf("PromptConfirm on EOF = (%v, %v), want (false, false)", approved, always)
	}
}
