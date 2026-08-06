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
// at startup. It is a var (not a const) so a release build can stamp it at
// link time via `-ldflags "-X main.version=..."` (see client/Makefile and
// docs/PACKAGING.md) without touching this source file.
var version = "0.3.0"

const (
	heartbeatInterval = 20 * time.Second
	minBackoff        = 1 * time.Second
	maxBackoff        = 30 * time.Second
)

// config holds the fully-resolved client configuration, whatever the source
// (flag, environment variable, or compiled-in buildDefaults) of each value.
// token is a *SecretBytes (not a plain string) so it can be zeroized in
// memory at shutdown — part of the residue-free runtime (docs/PLAN.md
// Phase 6).
type config struct {
	url          string
	token        *SecretBytes
	policy       Policy
	insecure     bool
	removeOnExit bool

	// identitySecret and ephemeralCode drive the stable-address feature
	// (client/identity.go, docs/PROTOCOL.md). identitySecret is the
	// resolved IDENTITY_SECRET (compiled default only — see buildDefaults;
	// there is deliberately no --identity-secret flag/env, since accepting
	// it from the command line would defeat the point of baking it into a
	// customized binary). ephemeralCode, when true, opts back into the
	// pre-existing random-code-per-connection behavior.
	identitySecret string
	ephemeralCode  bool
}

// buildDefaults holds the compiled-in fallback values a customized build
// stamps into buildconfig.go's package-level vars. Threading it through as
// a parameter (rather than parseConfigWithDefaults reading the package
// vars itself) is what keeps the three-level resolution logic a pure,
// unit-testable function of its inputs.
type buildDefaults struct {
	url            string
	token          string
	identitySecret string
}

func main() {
	cfg, err := parseConfig(os.Args[1:], os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "claude-distant-client:", err)
		os.Exit(2)
	}

	// Dedicated scratch directory for anything the client needs to write
	// to disk; removed in full at shutdown (see the RunGuarded call
	// below), including on panic. See workspace.go.
	ws, err := NewWorkspace()
	if err != nil {
		fmt.Fprintln(os.Stderr, "claude-distant-client:", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	fmt.Printf("claude-distant client v%s (%s/%s) — policy=%s\n", version, runtime.GOOS, runtime.GOARCH, cfg.policy)
	fmt.Println("Connexion au relay...")

	// RunGuarded guarantees the cleanup below runs exactly once no matter
	// how runUI exits — clean return, error return, or panic — so the
	// workspace is always removed and the token always zeroized, and
	// (opt-in) the binary itself is always deleted. runUI is the
	// build-tag-selected entry point (gui_stub.go's console pass-through by
	// default, gui.go's Fyne window under `-tags gui`); this file
	// deliberately knows nothing about Fyne or which tag is active.
	var runErr error
	RunGuarded(func() {
		ws.Cleanup()
		cfg.token.Zero()
		if cfg.removeOnExit {
			removeOwnBinary()
		}
	}, func() {
		runErr = runUI(ctx, cfg, ws)
	})

	if runErr != nil && runErr != context.Canceled {
		fmt.Fprintln(os.Stderr, "claude-distant-client: arrêt:", runErr)
		os.Exit(1)
	}
	fmt.Println("Arrêt propre du client.")
}

// removeOwnBinary resolves the running binary's own path and best-effort
// deletes it (see cleanup_binary.go). Failures are reported but never
// fatal: removing the binary is a best-effort cleanup convenience, not a
// security boundary, and must never prevent an otherwise-clean shutdown.
func removeOwnBinary() {
	path, err := resolveExecutablePath(os.Executable)
	if err != nil {
		fmt.Fprintln(os.Stderr, "claude-distant-client: remove-on-exit: chemin introuvable:", err)
		return
	}
	if err := removeBinary(runtime.GOOS, path, os.Getpid()); err != nil {
		fmt.Fprintln(os.Stderr, "claude-distant-client: remove-on-exit: suppression échouée:", err)
		return
	}
	fmt.Println("Nettoyage: binaire supprimé.")
}

// parseConfig resolves flags and environment variables into a config, using
// this package's own compiled-in buildDefaults (buildconfig.go) as the
// third and last resolution level. This is the entry point main() calls;
// its behavior for a generic (non-customized) build — buildRelayURL /
// buildClientToken / buildIdentitySecret all "" — is byte-for-byte
// unchanged from before compiled defaults existed, which is the
// non-regression pinned by TestParseConfig_GenericBuildStillRequiresURLAndToken.
func parseConfig(args []string, getenv func(string) string) (config, error) {
	return parseConfigWithDefaults(args, getenv, buildDefaults{
		url:            buildRelayURL,
		token:          buildClientToken,
		identitySecret: buildIdentitySecret,
	})
}

// parseConfigWithDefaults is parseConfig's testable core. Each setting is
// resolved with the first non-empty source winning, in this order:
//  1. the command-line flag (--url, --token)
//  2. the CLAUDE_DISTANT_* environment variable
//  3. defaults, compiled into the binary at link time
//
// The "--url (ou CLAUDE_DISTANT_URL) est requis" family of errors is only
// raised once all three sources come up empty, so a customized build (all
// three defaults set) launches with zero arguments, while a generic build
// (defaults all "") behaves exactly as it always has. It is a pure function
// of (args, getenv, defaults) so it can be unit tested without touching the
// real process environment or this package's compiled-in vars.
func parseConfigWithDefaults(args []string, getenv func(string) string, defaults buildDefaults) (config, error) {
	fs := flag.NewFlagSet("claude-distant-client", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	urlFlag := fs.String("url", "", "URL WebSocket du relay (ex: wss://relay.example.com/ws/client)")
	tokenFlag := fs.String("token", "", "Jeton Bearer pré-configuré du client")
	policyFlag := fs.String("policy", "", "Politique de garde-fou : auto|confirm|deny")
	insecureFlag := fs.Bool("insecure-skip-verify", false, "Désactive la vérification TLS (développement uniquement)")
	removeOnExitFlag := fs.Bool("remove-on-exit", false, "Supprime le binaire lui-même à l'arrêt propre (best-effort, désactivé par défaut)")
	ephemeralCodeFlag := fs.Bool("ephemeral-code", false, "Revient au code de session aléatoire (au lieu de l'adresse stable dérivée de la machine)")

	if err := fs.Parse(args); err != nil {
		return config{}, err
	}

	url := *urlFlag
	if url == "" {
		url = getenv("CLAUDE_DISTANT_URL")
	}
	if url == "" {
		url = defaults.url
	}
	if strings.TrimSpace(url) == "" {
		return config{}, fmt.Errorf("--url (ou CLAUDE_DISTANT_URL) est requis")
	}

	token := *tokenFlag
	if token == "" {
		token = getenv("CLAUDE_DISTANT_TOKEN")
	}
	if token == "" {
		token = defaults.token
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

	return config{
		url:            url,
		token:          NewSecret(token),
		policy:         policy,
		insecure:       *insecureFlag,
		removeOnExit:   removeOnExitEnabled(*removeOnExitFlag, getenv),
		identitySecret: defaults.identitySecret,
		ephemeralCode:  ephemeralCodeEnabled(*ephemeralCodeFlag, getenv),
	}, nil
}

// ephemeralCodeEnabled resolves the --ephemeral-code flag / the
// CLAUDE_DISTANT_EPHEMERAL_CODE env var into a single boolean decision,
// mirroring removeOnExitEnabled's flag-or-truthy-env pattern
// (cleanup_binary.go).
func ephemeralCodeEnabled(flagSet bool, getenv func(string) string) bool {
	if flagSet {
		return true
	}
	return isTruthy(getenv("CLAUDE_DISTANT_EPHEMERAL_CODE"))
}

// runForever maintains the connection to the relay, reconnecting with
// exponential backoff (with jitter) until ctx is cancelled (e.g. Ctrl-C).
func runForever(ctx context.Context, cfg config, ws *Workspace) error {
	backoff := minBackoff
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		err := runSession(ctx, cfg, ws)
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
func runSession(ctx context.Context, cfg config, ws *Workspace) error {
	conn, err := DialRelay(ctx, cfg.url, cfg.token.String(), cfg.insecure)
	if err != nil {
		return err
	}
	defer conn.Close()

	hostname, _ := os.Hostname()
	desiredCode := resolveDesiredCode(cfg, MachineID)
	if err := conn.WriteJSON(NewRegisterMessage(runtime.GOOS, hostname, version, ClientCapabilities, desiredCode)); err != nil {
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
	confirmFn := func(command string) (bool, bool) { return PromptConfirm(stdin, command) }
	// events is nil here: the console entry point (this file) has no
	// activity log to feed and must not gain any stdout noise from one —
	// a non-regression this package's tests pin down explicitly. The GUI
	// entry point (client/gui.go, out of this task's scope) is expected to
	// build its own Executor with a non-nil sink instead.
	executor := NewExecutor(conn, NewPolicyController(cfg.policy), confirmFn, ws.Dir(), nil)

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
			// Exécuter la commande dans une goroutine : la boucle de
			// lecture DOIT rester libre de lire les autres messages du
			// relay pendant l'exécution. Sinon, une commande longue bloque
			// la lecture des `heartbeat_ack`, le read deadline (wsconn.go)
			// finit par expirer et la connexion tombe — et un éventuel
			// prompt de confirmation (policy=confirm) gèlerait tout le
			// canal. Le seul état partagé mutable de Handle (la liste des
			// commandes "toujours autorisées") est protégé par mutex, et
			// l'écriture passe par conn.WriteJSON (protégé par mutex lui
			// aussi), donc l'exécution concurrente de plusieurs commandes
			// est sûre.
			go executor.Handle(sessionCtx, m)
		case TypeHeartbeatAck:
			// no-op: ReadEnvelope already refreshed the read deadline.
		default:
			log.Printf("message inconnu reçu du relay: %s", msgType)
		}
	}
}

// resolveDesiredCode computes the register message's desired_code field
// (docs/PROTOCOL.md, client/identity.go): HMAC-derived from this
// machine's MachineID and the (possibly compiled-in) identity secret,
// unless --ephemeral-code opted back into the old random-code-per-connection
// behavior. machineID is injected (MachineID in production) so this stays
// testable without touching the real machine. A MachineID failure degrades
// to "" (no desired_code sent) rather than a fatal error — exactly what an
// old, pre-stable-address client did, and the relay's documented fallback
// (docs/PROTOCOL.md) already covers that case.
func resolveDesiredCode(cfg config, machineID func() (string, error)) string {
	if cfg.ephemeralCode {
		return ""
	}
	id, err := machineID()
	if err != nil || id == "" {
		return ""
	}
	secret := cfg.identitySecret
	if secret == "" {
		secret = defaultIdentitySalt
	}
	return DeriveSessionCode(secret, id)
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
