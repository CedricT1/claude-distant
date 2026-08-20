//go:build gui

package main

// This file implements the desktop GUI (client/README.md, SPEC-gui-identity.md
// §4). It is only compiled into a binary built with `-tags gui`
// (client/Makefile's `dist-gui` target): Fyne links X11/OpenGL dynamically on
// Linux, so a GUI binary refuses to even start on a display-less server,
// which is exactly why the console build (gui_stub.go, no tag) stays the
// default deliverable and this code path is opt-in.
//
// runUI below is the GUI's counterpart to gui_stub.go's pass-through: same
// signature, called the same way from main.go, but it owns its own
// connect/reconnect loop (guiRunForever/guiRunSession) instead of reusing
// main.go's runForever/runSession. Those two are wired specifically for the
// console — a fresh *PolicyController per reconnect, a blocking os.Stdin
// prompt, no activity log — none of which fits a long-lived window whose
// "mode automatique" switch and live log must survive a dropped connection.
// Duplicating the reconnect loop's shape here (rather than reaching into
// main.go) is what keeps main.go itself free of any Fyne/tag knowledge, per
// this task's scope.

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"runtime"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// Connection-status strings shown in the header (French, matching the rest
// of the client's user-facing text).
const (
	statusConnecting   = "reconnexion en cours…"
	statusConnected    = "connecté"
	statusDisconnected = "déconnecté"
)

// activityLogCapacity mirrors docs/PROTOCOL.md's suggested bound for a
// session-long log (see activitylog.go's NewActivityLog doc).
const activityLogCapacity = 5000

// runUI builds and shows the main window, then hands off to Fyne's own event
// loop (ShowAndRun blocks until the window closes). The relay connection
// itself runs on a background goroutine (guiRunForever) started before that
// call, exactly like the console build's runForever runs alongside main()'s
// signal handling — the window is just another way to observe and drive the
// same session.
func runUI(ctx context.Context, cfg config, ws *Workspace) error {
	uiCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	a := app.New()
	w := a.NewWindow("claude-distant client")
	w.Resize(fyne.NewSize(720, 560))
	// Closing the window (or the OS signal ctx already carries) both mean
	// "shut down the session": wire the window close to the same cancel
	// that a Ctrl-C would trigger, so either path unblocks guiRunSession's
	// read loop and lets it return.
	w.SetOnClosed(cancel)

	g := newGUIState(w, NewPolicyController(cfg.policy), NewActivityLog(activityLogCapacity))
	g.build(cfg.policy == PolicyAuto)

	// ctx (not uiCtx) here: if the process receives a shutdown signal while
	// the window is still open, close the window too so ShowAndRun returns
	// and main()'s cleanup can proceed instead of waiting on a window the
	// user never touched.
	go func() {
		<-ctx.Done()
		fyne.Do(func() { w.Close() })
	}()

	go func() {
		if err := guiRunForever(uiCtx, cfg, ws, g); err != nil && err != context.Canceled {
			log.Println("claude-distant-client:", err)
		}
	}()

	w.ShowAndRun()
	return nil
}

// guiRunForever is runForever's (main.go) GUI counterpart: same
// reconnect-with-backoff shape, but reports connection state to the window
// instead of stderr, and threads through one long-lived PolicyController and
// ActivityLog for the whole process lifetime instead of the console build's
// per-session ones.
func guiRunForever(ctx context.Context, cfg config, ws *Workspace, g *guiState) error {
	backoff := minBackoff
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		fyne.Do(func() { g.setStatus(statusConnecting) })
		err := guiRunSession(ctx, cfg, ws, g)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		fyne.Do(func() { g.setStatus(statusDisconnected) })
		if err != nil {
			g.activityLog.Append(ActivityEvent{Time: time.Now(), Kind: ActivityConnection, Detail: fmt.Sprintf("connexion perdue: %v", err)})
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

// guiRunSession is runSession's (main.go) GUI counterpart. The differences
// from the console version are exactly the GUI's three requirements: the
// operator-confirm prompt is g.confirm (a Fyne dialog) instead of stdin, the
// policy is the long-lived g.policy instead of a fresh PolicyController, and
// every Executor event feeds g.activityLog instead of being dropped (nil).
func guiRunSession(ctx context.Context, cfg config, ws *Workspace, g *guiState) error {
	conn, err := DialRelay(ctx, cfg.url, cfg.token.String(), cfg.insecure)
	if err != nil {
		return err
	}
	defer conn.Close()

	hostname, _ := os.Hostname()
	fyne.Do(func() { g.setIdentity(hostname, runtime.GOOS) })

	desiredCode := resolveDesiredCode(cfg, MachineID)
	if err := conn.WriteJSON(NewRegisterMessage(runtime.GOOS, hostname, version, ClientCapabilities, desiredCode)); err != nil {
		return fmt.Errorf("envoi register: %w", err)
	}

	sessionCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	// Unblock the read loop promptly on shutdown, exactly like runSession
	// (main.go): closing the connection makes the in-flight ReadEnvelope()
	// return an error immediately instead of waiting out the read deadline.
	go func() {
		<-sessionCtx.Done()
		_ = conn.Close()
	}()

	executor := NewExecutor(conn, g.policy, g.confirm, ws.Dir(), g.activityLog.Append)

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
				fyne.Do(func() {
					g.setSessionCode(m.SessionCode)
					g.setStatus(statusConnected)
				})
				g.activityLog.Append(ActivityEvent{Time: time.Now(), Kind: ActivityConnection, Detail: "connecté, code de session " + m.SessionCode})
			}
		case TypeCommand:
			var m CommandMessage
			if jsonErr := json.Unmarshal(data, &m); jsonErr != nil {
				log.Printf("message command invalide: %v", jsonErr)
				continue
			}
			// Same reasoning as runSession (main.go): the read loop must
			// stay free to keep reading heartbeat_ack / other commands
			// while this one runs, and a confirm-policy prompt must not
			// freeze the whole channel. Executor's only shared mutable
			// state is mutex-protected, and conn.WriteJSON is itself
			// mutex-protected, so concurrent Handle calls are safe.
			go executor.Handle(sessionCtx, m)
		case TypeHeartbeatAck:
			// no-op: ReadEnvelope already refreshed the read deadline.
		default:
			log.Printf("message inconnu reçu du relay: %s", msgType)
		}
	}
}

// guiState bundles every long-lived widget/object the reconnect loop above
// and the window's own callbacks need to reach: the single value threaded
// through instead of package-level globals, which would make more than one
// runUI call (e.g. from a future test) unsafe to run in the same process.
type guiState struct {
	window fyne.Window

	policy      *PolicyController
	activityLog *ActivityLog

	codeText      *canvas.Text
	rawCode       string
	statusLabel   *widget.Label
	identityLabel *widget.Label
	warningRow    fyne.CanvasObject

	logList *widget.List
	// logEvents is every event the window has received (bounded exactly like
	// the ActivityLog it mirrors), logLines the subset currently displayed
	// — the two differ as soon as "commandes du harnais uniquement" is
	// ticked. Both are touched only on the Fyne UI goroutine (widget
	// callbacks, or via fyne.Do), so no separate mutex is needed; see
	// build().
	logEvents []ActivityEvent
	// logShown / logLines are the displayed subset, kept index-aligned: the
	// list renders logLines[i], and a tap on row i opens logShown[i] in the
	// detail dialog (which shows the full RFC3339 line the compact row
	// abbreviates).
	logShown []ActivityEvent
	logLines []string
	// commandsOnly filters the panel down to what the harness asked for
	// (ActivityCommand), hiding the approvals/results/connection chatter
	// around it. It changes the VIEW only: logEvents keeps everything, and
	// "Enregistrer le log…" still writes the whole journal.
	commandsOnly bool

	fullScreen bool
	expandBtn  *widget.Button
}

// newGUIState allocates a guiState; build() (called once, right after)
// populates its widgets and wires them to policy/activityLog.
func newGUIState(w fyne.Window, policy *PolicyController, log *ActivityLog) *guiState {
	return &guiState{window: w, policy: policy, activityLog: log}
}

// build constructs every widget described in SPEC-gui-identity.md §4 and
// sets w's content. initialAuto seeds the "mode automatique" switch from the
// policy the client was launched with (cfg.policy == PolicyAuto).
func (g *guiState) build(initialAuto bool) {
	// --- Header: session code (very large, grouped), copy button, status,
	// hostname/OS. ---
	g.codeText = canvas.NewText("--- --- ---", theme.Color(theme.ColorNameForeground))
	g.codeText.TextSize = 42
	g.codeText.TextStyle = fyne.TextStyle{Bold: true, Monospace: true}
	g.codeText.Alignment = fyne.TextAlignCenter

	copyBtn := widget.NewButtonWithIcon("Copier", theme.ContentCopyIcon(), func() {
		if g.rawCode == "" {
			return
		}
		g.window.Clipboard().SetContent(g.rawCode)
	})

	g.statusLabel = widget.NewLabel(statusDisconnected)
	g.identityLabel = widget.NewLabel("—")

	header := container.NewVBox(
		container.NewCenter(container.NewHBox(g.codeText, copyBtn)),
		container.NewHBox(widget.NewLabelWithStyle("État :", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}), g.statusLabel),
		container.NewHBox(widget.NewLabelWithStyle("Machine :", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}), g.identityLabel),
	)

	// --- Control: automatic-mode switch and its permanent warning. ---
	warningIcon := widget.NewIcon(theme.WarningIcon())
	warningText := widget.NewLabel("Mode automatique actif : aucune commande ni opération fichier ne sera plus soumise à confirmation.")
	warningText.Wrapping = fyne.TextWrapWord
	warningRow := container.NewHBox(warningIcon, warningText)
	warningRow.Hidden = !initialAuto
	g.warningRow = warningRow

	autoCheck := widget.NewCheck("Mode automatique — ne plus demander de confirmation", func(checked bool) {
		if checked {
			g.policy.Set(PolicyAuto)
			g.warningRow.Show()
		} else {
			g.policy.Set(PolicyConfirm)
			g.warningRow.Hide()
		}
	})
	autoCheck.SetChecked(initialAuto)

	controls := container.NewVBox(autoCheck, warningRow)

	top := container.NewVBox(header, widget.NewSeparator(), controls)

	// --- Bottom: live activity log, resizable via VSplit, plus its two
	// buttons. widget.List (not a growing Entry/RichText) is deliberate: it
	// only renders the visible rows, so appending one line is O(1)
	// amortized instead of re-rendering the whole session's text on every
	// event — see the Subscribe callback below.
	g.logList = widget.NewList(
		func() int { return len(g.logLines) },
		func() fyne.CanvasObject {
			l := widget.NewLabel("")
			// Monospace: these lines are mostly shell commands and paths,
			// which are read character by character. Truncation rather than
			// wrapping: widget.List lays out uniform-height rows, so a
			// wrapped line would be clipped mid-height instead of growing
			// the row — a command wider than the window stays fully
			// readable through the detail dialog below (OnSelected).
			l.TextStyle = fyne.TextStyle{Monospace: true}
			l.Truncation = fyne.TextTruncateEllipsis
			return l
		},
		func(id widget.ListItemID, obj fyne.CanvasObject) {
			obj.(*widget.Label).SetText(g.logLines[id])
		},
	)
	g.logList.OnSelected = func(id widget.ListItemID) {
		g.showLogEntry(id)
		// A log row is a thing to read, not a selection to keep: clear it
		// so the same row can be tapped again right away.
		g.logList.Unselect(id)
	}

	g.expandBtn = widget.NewButtonWithIcon("Agrandir", theme.ViewFullScreenIcon(), func() {
		g.fullScreen = !g.fullScreen
		g.window.SetFullScreen(g.fullScreen)
		if g.fullScreen {
			g.expandBtn.SetText("Réduire")
		} else {
			g.expandBtn.SetText("Agrandir")
		}
	})
	saveBtn := widget.NewButtonWithIcon("Enregistrer le log…", theme.DocumentSaveIcon(), func() {
		g.saveLog()
	})
	commandsOnlyCheck := widget.NewCheck("Commandes du harnais uniquement", func(checked bool) {
		g.commandsOnly = checked
		g.rebuildLogLines()
	})
	logToolbar := container.NewHBox(g.expandBtn, saveBtn, commandsOnlyCheck)
	logHeader := container.NewHBox(widget.NewLabelWithStyle("Journal d'activité", fyne.TextAlignLeading, fyne.TextStyle{Bold: true}), logToolbar)
	logPane := container.NewBorder(logHeader, nil, nil, nil, g.logList)

	// Seed from anything already logged (empty for a fresh ActivityLog
	// today, but Events() is the documented way to read a backlog, and
	// keeping this general costs nothing).
	g.logEvents = append(g.logEvents, g.activityLog.Events()...)
	g.rebuildLogLines()

	g.activityLog.Subscribe(func(e ActivityEvent) {
		// Subscribe fires on whichever goroutine emitted the event (an
		// Executor command goroutine), so every touch of the widgets and of
		// logEvents/logLines is queued onto the Fyne UI goroutine here —
		// that single-threading is what lets those slices go unguarded.
		fyne.Do(func() { g.appendLogEvent(e) })
	})

	split := container.NewVSplit(top, logPane)
	split.SetOffset(0.35)

	g.window.SetContent(split)
}

// appendLogEvent records one new event and, if the current filter shows it,
// puts it on screen. Must run on the Fyne UI goroutine.
//
// logEvents is bounded exactly like the ActivityLog it mirrors: a window
// left open across a long session (or many reconnects) must not accumulate
// events the log itself has already evicted.
func (g *guiState) appendLogEvent(e ActivityEvent) {
	g.logEvents = append(g.logEvents, e)
	if over := len(g.logEvents) - activityLogCapacity; over > 0 {
		g.logEvents = append(g.logEvents[:0:0], g.logEvents[over:]...)
		// Eviction shifts every index, so the displayed subset is rebuilt
		// wholesale rather than patched — it happens once per event only
		// after a full session's worth of activity.
		g.rebuildLogLines()
		return
	}
	if !g.showsEvent(e) {
		return
	}
	g.logShown = append(g.logShown, e)
	g.logLines = append(g.logLines, renderActivityLine(e))
	g.logList.Refresh()
	g.logList.ScrollToBottom()
}

// rebuildLogLines recomputes the displayed lines from logEvents under the
// current filter — what the "commandes du harnais uniquement" checkbox and
// an eviction both need. Must run on the Fyne UI goroutine.
func (g *guiState) rebuildLogLines() {
	g.logShown = g.logShown[:0]
	g.logLines = g.logLines[:0]
	for _, e := range g.logEvents {
		if g.showsEvent(e) {
			g.logShown = append(g.logShown, e)
			g.logLines = append(g.logLines, renderActivityLine(e))
		}
	}
	g.logList.Refresh()
	if len(g.logLines) > 0 {
		g.logList.ScrollToBottom()
	}
}

// showsEvent reports whether e belongs in the panel under the current
// filter. Unfiltered, everything shows; filtered, only what the harness
// asked this client to do (executor.go emits exactly one ActivityCommand
// per inbound `command` message, so the filtered view is a faithful,
// one-line-per-request list of the harness's orders).
func (g *guiState) showsEvent(e ActivityEvent) bool {
	return !g.commandsOnly || e.Kind == ActivityCommand
}

// showLogEntry opens one log line in a dialog, where it can be read in full
// (wrapped over as many lines as it needs, unlike the truncated list row)
// and copied. Must run on the Fyne UI goroutine — OnSelected already does.
func (g *guiState) showLogEntry(id widget.ListItemID) {
	if id < 0 || id >= len(g.logShown) {
		return // the list was rebuilt (filter toggled, eviction) under the tap
	}
	// The full line, dated: this dialog is where a long command gets read in
	// full and copied out (into a report, a ticket), so it carries the same
	// text as the saved log rather than the row's abbreviated form.
	line := renderActivityLineFull(g.logShown[id])

	text := widget.NewLabel(line)
	text.TextStyle = fyne.TextStyle{Monospace: true}
	text.Wrapping = fyne.TextWrapWord

	d := dialog.NewCustomWithoutButtons("Détail", container.NewVScroll(text), g.window)
	copyBtn := widget.NewButtonWithIcon("Copier", theme.ContentCopyIcon(), func() {
		g.window.Clipboard().SetContent(line)
	})
	closeBtn := widget.NewButton("Fermer", func() { d.Hide() })
	d.SetButtons([]fyne.CanvasObject{copyBtn, closeBtn})
	d.Resize(fyne.NewSize(600, 260))
	d.Show()
}

// renderActivityLine formats one ActivityEvent for the live log panel: same
// fields, same order as ActivityLog.Render()'s saved-file lines
// (activitylog.go), with the timestamp abbreviated to the time of day. That
// one deliberate difference buys ~15 characters of row width for what the
// operator actually came to read — the command itself — and loses nothing:
// a session lasts minutes, so the date is redundant on screen, and both the
// detail dialog (showLogEntry) and the saved file keep the full RFC3339
// timestamp.
func renderActivityLine(e ActivityEvent) string {
	return fmt.Sprintf("%s [%s] %s", e.Time.Format("15:04:05"), e.Kind, e.Detail)
}

// setStatus updates the connection-status label. Must be called from the
// Fyne UI goroutine (i.e. wrapped in fyne.Do by every caller outside build).
func (g *guiState) setStatus(status string) {
	g.statusLabel.SetText(status)
}

// setIdentity updates the hostname/OS label. Must be called from the Fyne UI
// goroutine.
func (g *guiState) setIdentity(hostname, osName string) {
	g.identityLabel.SetText(fmt.Sprintf("%s (%s)", hostname, osName))
}

// setSessionCode updates the big header code and its Copy button's source
// text. Must be called from the Fyne UI goroutine.
func (g *guiState) setSessionCode(code string) {
	g.rawCode = code
	g.codeText.Text = formatSessionCode(code)
	g.codeText.Refresh()
}

// saveLog opens a native "save as" dialog and writes ActivityLog.Render()'s
// text to the chosen file. Runs synchronously on the button-tap callback,
// which Fyne already invokes on the UI goroutine, so no fyne.Do is needed
// here.
func (g *guiState) saveLog() {
	fd := dialog.NewFileSave(func(writer fyne.URIWriteCloser, err error) {
		if err != nil {
			dialog.ShowError(err, g.window)
			return
		}
		if writer == nil {
			return // operator cancelled the dialog
		}
		defer writer.Close()
		if _, err := writer.Write([]byte(g.activityLog.Render())); err != nil {
			dialog.ShowError(err, g.window)
		}
	}, g.window)
	fd.SetFileName("claude-distant-activity.log")
	fd.Show()
}

// confirm implements the Executor's confirm callback (executor.go's
// NewExecutor parameter): it must BLOCK the calling goroutine — one of
// guiRunSession's per-command goroutines, never the read loop itself — until
// the operator answers, while the three-button dialog it shows can only be
// built and driven from the Fyne UI goroutine. fyne.Do bridges the two: it
// queues the dialog's construction onto the UI goroutine and returns
// immediately, and this method blocks on a channel that one of the dialog's
// button callbacks (also running on the UI goroutine) closes over and
// writes to when the operator answers.
func (g *guiState) confirm(command string) (approved, always bool) {
	type answer struct{ approved, always bool }
	ch := make(chan answer, 1)
	respond := func(a answer) {
		select {
		case ch <- a:
		default:
			// Already answered (e.g. a button tap and the dialog's own
			// Hide()-triggered OnClosed both fire): drop the duplicate
			// rather than block or panic on a full channel.
		}
	}

	fyne.Do(func() {
		msg := widget.NewLabel(fmt.Sprintf("Le harnais veut exécuter :\n\n%s", command))
		msg.Wrapping = fyne.TextWrapWord

		d := dialog.NewCustomWithoutButtons("Confirmation requise", msg, g.window)
		refuseBtn := widget.NewButton("Refuser", func() { respond(answer{false, false}); d.Hide() })
		allowBtn := widget.NewButton("Autoriser", func() { respond(answer{true, false}); d.Hide() })
		alwaysBtn := widget.NewButton("Toujours", func() { respond(answer{true, true}); d.Hide() })
		d.SetButtons([]fyne.CanvasObject{refuseBtn, allowBtn, alwaysBtn})
		// Dismissing the dialog any other way (Esc, window manager close)
		// must resolve confirm() too, or the calling goroutine would block
		// forever: treat that exactly like "Refuser".
		d.SetOnClosed(func() { respond(answer{false, false}) })
		d.Show()
	})

	a := <-ch
	return a.approved, a.always
}
