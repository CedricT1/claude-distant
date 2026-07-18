package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"
)

// version is reported to the relay in the `register` message and printed
// at startup.
const version = "0.1.0"

const (
	heartbeatInterval = 20 * time.Second
	minBackoff        = 1 * time.Second
	maxBackoff        = 30 * time.Second
)

// config holds the fully-resolved client configuration, whatever the source
// (flag or environment variable) of each value.
type config struct {
	url      string
	token    string
	policy   Policy
	insecure bool
}

func main() {
	cfg, err := parseConfig(os.Args[1:], os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "claude-distant-client:", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	fmt.Printf("claude-distant client v%s (%s/%s) — policy=%s\n", version, runtime.GOOS, runtime.GOARCH, cfg.policy)
	fmt.Println("Connexion au relay...")

	if err := runForever(ctx, cfg); err != nil && err != context.Canceled {
		fmt.Fprintln(os.Stderr, "claude-distant-client: arrêt:", err)
		os.Exit(1)
	}
	fmt.Println("Arrêt propre du client.")
}

// parseConfig resolves flags and environment variables into a config, with
// flags taking precedence over the matching CLAUDE_DISTANT_* env var. It is
// a pure function of (args, getenv) so it can be unit tested without
// touching the real process environment.
func parseConfig(args []string, getenv func(string) string) (config, error) {
	fs := flag.NewFlagSet("claude-distant-client", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	urlFlag := fs.String("url", "", "URL WebSocket du relay (ex: wss://relay.example.com/ws/client)")
	tokenFlag := fs.String("token", "", "Jeton Bearer pré-configuré du client")
	policyFlag := fs.String("policy", "", "Politique de garde-fou : auto|confirm|deny")
	insecureFlag := fs.Bool("insecure-skip-verify", false, "Désactive la vérification TLS (développement uniquement)")

	if err := fs.Parse(args); err != nil {
		return config{}, err
	}

	url := *urlFlag
	if url == "" {
		url = getenv("CLAUDE_DISTANT_URL")
	}
	if strings.TrimSpace(url) == "" {
		return config{}, fmt.Errorf("--url (ou CLAUDE_DISTANT_URL) est requis")
	}

	token := *tokenFlag
	if token == "" {
		token = getenv("CLAUDE_DISTANT_TOKEN")
	}
	if strings.TrimSpace(token) == "" {
		return config{}, fmt.Errorf("--token (ou CLAUDE_DISTANT_TOKEN) est requis")
	}

	policyStr := *policyFlag
	if policyStr == "" {
		policyStr = getenv("CLAUDE_DISTANT_POLICY")
	}
	if policyStr == "" {
		policyStr = string(PolicyConfirm)
	}
	policy, err := ParsePolicy(policyStr)
	if err != nil {
		return config{}, err
	}

	return config{url: url, token: token, policy: policy, insecure: *insecureFlag}, nil
}

// runForever maintains the connection to the relay, reconnecting with
// exponential backoff (with jitter) until ctx is cancelled (e.g. Ctrl-C).
func runForever(ctx context.Context, cfg config) error {
	backoff := minBackoff
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		err := runSession(ctx, cfg)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err != nil {
			fmt.Fprintf(os.Stderr, "connexion perdue (%v), nouvelle tentative dans %s\n", err, backoff)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(jitter(backoff)):
		}
		backoff *= 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
}

// jitter returns a randomized duration in [d/2, d) to avoid reconnect storms.
func jitter(d time.Duration) time.Duration {
	half := d / 2
	if half <= 0 {
		return d
	}
	return half + time.Duration(rand.Int63n(int64(half)))
}

// runSession opens one WebSocket connection, registers, and processes
// messages until the connection drops or ctx is cancelled.
func runSession(ctx context.Context, cfg config) error {
	conn, err := DialRelay(ctx, cfg.url, cfg.token, cfg.insecure)
	if err != nil {
		return err
	}
	defer conn.Close()

	hostname, _ := os.Hostname()
	if err := conn.WriteJSON(NewRegisterMessage(runtime.GOOS, hostname, version)); err != nil {
		return fmt.Errorf("envoi register: %w", err)
	}

	sessionCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	// Unblock the read loop promptly on shutdown: closing the connection
	// makes the in-flight ReadEnvelope() return an error immediately.
	go func() {
		<-sessionCtx.Done()
		_ = conn.Close()
	}()

	stdin := bufio.NewReader(os.Stdin)
	confirmFn := func(command string) bool { return PromptConfirm(stdin, command) }
	executor := NewExecutor(conn, cfg.policy, confirmFn)

	go heartbeatLoop(sessionCtx, conn)

	for {
		msgType, data, err := conn.ReadEnvelope()
		if err != nil {
			return err
		}

		switch msgType {
		case TypeRegistered:
			var m RegisteredMessage
			if jsonErr := json.Unmarshal(data, &m); jsonErr == nil {
				printSessionCode(m.SessionCode)
			}
		case TypeCommand:
			var m CommandMessage
			if jsonErr := json.Unmarshal(data, &m); jsonErr != nil {
				log.Printf("message command invalide: %v", jsonErr)
				continue
			}
			executor.Handle(sessionCtx, m)
		case TypeHeartbeatAck:
			// no-op: ReadEnvelope already refreshed the read deadline.
		default:
			log.Printf("message inconnu reçu du relay: %s", msgType)
		}
	}
}

func heartbeatLoop(ctx context.Context, conn *Conn) {
	ticker := time.NewTicker(heartbeatInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = conn.WriteJSON(NewHeartbeatMessage())
		}
	}
}

// printSessionCode displays the 9-digit session code grouped as "784 123 678"
// so the local user can read it out to the operator.
func printSessionCode(code string) {
	fmt.Println()
	fmt.Println("========================================")
	fmt.Printf("  Code de session : %s\n", formatSessionCode(code))
	fmt.Println("  Communiquez ce code à l'opérateur.")
	fmt.Println("========================================")
	fmt.Println()
}

// formatSessionCode groups a 9-digit code as "XXX XXX XXX". Non-digit
// characters are stripped first (defensive); inputs that aren't exactly 9
// digits after stripping are returned unchanged.
func formatSessionCode(code string) string {
	var digits strings.Builder
	for _, r := range code {
		if r >= '0' && r <= '9' {
			digits.WriteRune(r)
		}
	}
	d := digits.String()
	if len(d) != 9 {
		return code
	}
	return d[0:3] + " " + d[3:6] + " " + d[6:9]
}
