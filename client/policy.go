package main

import (
	"bufio"
	"fmt"
	"regexp"
	"strings"
	"sync"
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

// PolicyController holds the local guard-rail Policy as a value that can be
// changed while the client is running — e.g. the GUI's "mode automatique"
// switch (client/gui.go) toggling between PolicyAuto and PolicyConfirm — and
// consulted from a different goroutine than the one changing it. Executor
// holds a *PolicyController rather than a bare Policy and calls Get() at
// every guard-rail decision, so a Set() takes effect on the very next
// command, without restarting the session. Safe for concurrent use: a
// sync.RWMutex protects the value (RLock lets concurrent commands read the
// policy without blocking each other; Lock is only taken by the rarer
// Set()).
type PolicyController struct {
	mu sync.RWMutex
	p  Policy
}

// NewPolicyController creates a PolicyController initialized to p (typically
// the --policy flag's resolved value at startup).
func NewPolicyController(p Policy) *PolicyController {
	return &PolicyController{p: p}
}

// Get returns the current policy. Safe to call from any goroutine.
func (c *PolicyController) Get() Policy {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.p
}

// Set changes the current policy. Safe to call from any goroutine.
func (c *PolicyController) Set(p Policy) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.p = p
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
// docs/PROTOCOL.md and blocks until the operator answers. always reports
// whether the operator chose "toujours" (approve this exact command
// without prompting again for the rest of the session); it is only ever
// true when approved is also true.
func PromptConfirm(stdin *bufio.Reader, command string) (approved bool, always bool) {
	fmt.Println()
	fmt.Println("----------------------------------------")
	fmt.Printf("Le harnais veut exécuter :\n  %s\n", command)
	fmt.Print("[Autoriser/Refuser/Toujours] (o/N/t) : ")
	line, err := stdin.ReadString('\n')
	if err != nil {
		return false, false
	}
	switch strings.ToLower(strings.TrimSpace(line)) {
	case "o", "oui", "y", "yes":
		return true, false
	case "t", "toujours", "always", "a":
		return true, true
	default:
		return false, false
	}
}
