package main

import "testing"

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
