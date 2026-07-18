package main

import (
	"bufio"
	"fmt"
	"regexp"
	"strings"
)

// Policy is the local guard-rail mode the client was launched with
// (docs/PROTOCOL.md "Garde-fou local").
type Policy string

const (
	PolicyAuto    Policy = "auto"
	PolicyConfirm Policy = "confirm"
	PolicyDeny    Policy = "deny"
)

// ParsePolicy validates a --policy flag / CLAUDE_DISTANT_POLICY value.
// It is case-insensitive and trims surrounding whitespace.
func ParsePolicy(s string) (Policy, error) {
	switch Policy(strings.ToLower(strings.TrimSpace(s))) {
	case PolicyAuto:
		return PolicyAuto, nil
	case PolicyConfirm:
		return PolicyConfirm, nil
	case PolicyDeny:
		return PolicyDeny, nil
	default:
		return "", fmt.Errorf("politique invalide %q (attendu: auto|confirm|deny)", s)
	}
}

// destructivePatterns is a simple, documented, and easily extensible list of
// regular expressions used to flag a command as destructive. Matching is
// case-insensitive and intentionally coarse: a false positive (flagging a
// safe command) only costs an extra confirmation prompt, while a false
// negative could let a dangerous command slip through unreviewed — so these
// patterns are kept broad on purpose. Extend this slice to cover more cases.
var destructivePatterns = []*regexp.Regexp{
	// Recursive / forced deletion
	regexp.MustCompile(`(?i)\brm\s+.*-[a-z]*r[a-z]*f`),
	regexp.MustCompile(`(?i)\brm\s+.*-[a-z]*f[a-z]*r`),
	regexp.MustCompile(`(?i)\bremove-item\b.*-recurse`),
	regexp.MustCompile(`(?i)\bremove-item\b.*-force`),
	regexp.MustCompile(`(?i)\brd\s+/s\b`),
	regexp.MustCompile(`(?i)\brmdir\s+/s\b`),
	regexp.MustCompile(`(?i)\bdel\s+/s\b`),

	// Filesystem / disk destruction
	regexp.MustCompile(`(?i)\bmkfs(\.\w+)?\b`),
	regexp.MustCompile(`(?i)\bdd\s+.*\bof=`),
	regexp.MustCompile(`(?i)\bwipefs\b`),
	regexp.MustCompile(`(?i)\bshred\b`),
	regexp.MustCompile(`(?i)\bfdisk\b`),
	regexp.MustCompile(`(?i)\bparted\b`),
	regexp.MustCompile(`(?i)\bdiskpart\b`),
	regexp.MustCompile(`(?i)\bformat(-volume)?\b`),
	regexp.MustCompile(`(?i)\bclear-disk\b`),
	regexp.MustCompile(`(?i)>\s*/dev/(sd|nvme|hd|xvd)\w*\b`),

	// Power / shutdown
	regexp.MustCompile(`(?i)\bshutdown\b`),
	regexp.MustCompile(`(?i)\breboot\b`),
	regexp.MustCompile(`(?i)\bpoweroff\b`),
	regexp.MustCompile(`(?i)\brestart-computer\b`),
	regexp.MustCompile(`(?i)\bstop-computer\b`),

	// Accounts / registry / firewall
	regexp.MustCompile(`(?i)\buserdel\b`),
	regexp.MustCompile(`(?i)\bdeluser\b`),
	regexp.MustCompile(`(?i)\breg\s+delete\b`),
	regexp.MustCompile(`(?i)\biptables\s+-f\b`),

	// Fork bomb
	regexp.MustCompile(`:\s*\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:`),
}

// IsDestructive reports whether command matches any known destructive
// pattern. It is a best-effort heuristic used to decide whether the
// confirm/deny guard-rail engages — not a sandbox or security boundary.
func IsDestructive(command string) bool {
	for _, p := range destructivePatterns {
		if p.MatchString(command) {
			return true
		}
	}
	return false
}

// PromptConfirm shows the local guard-rail prompt described in
// docs/PROTOCOL.md and blocks until the operator answers.
func PromptConfirm(stdin *bufio.Reader, command string) bool {
	fmt.Println()
	fmt.Println("----------------------------------------")
	fmt.Printf("Le harnais veut exécuter :\n  %s\n", command)
	fmt.Print("[Autoriser/Refuser] (o/N) : ")
	line, err := stdin.ReadString('\n')
	if err != nil {
		return false
	}
	answer := strings.ToLower(strings.TrimSpace(line))
	return answer == "o" || answer == "oui" || answer == "y" || answer == "yes"
}
