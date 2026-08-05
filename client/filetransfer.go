package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const (
	// maxFileTransferBytes caps a single transfer in either direction. A
	// write travels relay->client inside ONE `command` frame carrying the
	// whole base64 payload, so this ceiling is what keeps a frame — and the
	// relay's memory — bounded. A read is chunked, but honours the same cap
	// so the harness cannot pull an arbitrarily large file in one call.
	maxFileTransferBytes = 8 << 20
	// fileChunkBytes is the raw slice size a read is cut into before being
	// base64-encoded into one `file_chunk` message (192 KiB -> 256 KiB on
	// the wire).
	fileChunkBytes = 192 * 1024
)

// Stable error codes reported to the harness through `result.error`. They are
// part of the protocol: the harness matches on them, never on the (localized,
// OS-dependent) message text.
const (
	errCodeFileNotFound     = "file_not_found"
	errCodePermissionDenied = "permission_denied"
	errCodeIsADirectory     = "is_a_directory"
	errCodeFileExists       = "file_exists"
	errCodeFileTooLarge     = "file_too_large"
	errCodeInvalidBase64    = "invalid_base64"
	errCodeInvalidParams    = "invalid_params"
	errCodeIOError          = "io_error"
)

// fileError pairs one of the stable codes above with the underlying cause,
// for the conditions we detect ourselves (an empty path, a refused overwrite,
// an oversized payload). Plain OS errors are returned as-is and classified by
// fileErrorCode.
type fileError struct {
	code string
	err  error
}

func (e *fileError) Error() string {
	if e.err == nil {
		return e.code
	}
	return e.code + ": " + e.err.Error()
}

func (e *fileError) Unwrap() error { return e.err }

func fileErrorf(code, format string, a ...any) error {
	return &fileError{code: code, err: fmt.Errorf(format, a...)}
}

// fileErrorCode maps a transfer error to its stable protocol code. OS errors
// are classified through errors.Is against the fs sentinels rather than by
// pattern-matching their text, which differs per platform and per locale.
func fileErrorCode(err error) string {
	if err == nil {
		return ""
	}
	var fe *fileError
	if errors.As(err, &fe) {
		return fe.code
	}
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return errCodeFileNotFound
	case errors.Is(err, fs.ErrPermission):
		return errCodePermissionDenied
	case errors.Is(err, fs.ErrExist):
		return errCodeFileExists
	default:
		return errCodeIOError
	}
}

// transferPath validates the path of a transfer. `~` is deliberately NOT
// expanded (documented in docs/PROTOCOL.md): the harness sends an absolute
// path, or one relative to the client's own working directory. There is no
// workspace confinement either — these tools exist for system administration.
func transferPath(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", fileErrorf(errCodeInvalidParams, "params.path manquant")
	}
	return path, nil
}

// readFileTransfer streams the requested slice of a file, handing each chunk
// to emit as base64 with an increasing seq, and returns the `meta` object of
// the final `result`: path, size (bytes actually sent), sha256 of those same
// bytes, and truncated (bytes remain past the limit). It stops at the first
// emit failure so a dropped connection never looks like a complete transfer.
func readFileTransfer(p ReadFileParams, emit func(seq int, data string) error) (map[string]any, error) {
	path, err := transferPath(p.Path)
	if err != nil {
		return nil, err
	}
	if p.Offset < 0 {
		return nil, fileErrorf(errCodeInvalidParams, "params.offset négatif: %d", p.Offset)
	}
	if p.MaxBytes < 0 {
		return nil, fileErrorf(errCodeInvalidParams, "params.max_bytes négatif: %d", p.MaxBytes)
	}

	// Stat first: opening a directory succeeds on Unix and only fails at the
	// first Read, with an errno we would then have to guess at.
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if info.IsDir() {
		return nil, fileErrorf(errCodeIsADirectory, "%s est un répertoire", path)
	}

	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	if p.Offset > 0 {
		if _, err := f.Seek(p.Offset, io.SeekStart); err != nil {
			return nil, err
		}
	}

	limit := int64(maxFileTransferBytes)
	if p.MaxBytes > 0 && p.MaxBytes < limit {
		limit = p.MaxBytes
	}

	hasher := sha256.New()
	buf := make([]byte, fileChunkBytes)
	var sent int64
	seq := 0
	reachedEOF := false

	for sent < limit {
		n := int64(fileChunkBytes)
		if remaining := limit - sent; remaining < n {
			n = remaining
		}
		read, readErr := io.ReadFull(f, buf[:n])
		if read > 0 {
			hasher.Write(buf[:read])
			sent += int64(read)
			if emitErr := emit(seq, base64.StdEncoding.EncodeToString(buf[:read])); emitErr != nil {
				return nil, emitErr
			}
			seq++
		}
		if readErr != nil {
			// A short final read is the normal end of file, not a failure.
			if errors.Is(readErr, io.EOF) || errors.Is(readErr, io.ErrUnexpectedEOF) {
				reachedEOF = true
				break
			}
			return nil, readErr
		}
	}

	truncated := false
	if !reachedEOF {
		// We stopped on the limit, not on the end of the file: probe one
		// extra byte to tell "exactly the whole file" from "there is more",
		// which is what tells the harness to issue a follow-up read.
		var probe [1]byte
		if n, _ := f.Read(probe[:]); n > 0 {
			truncated = true
		}
	}

	return map[string]any{
		"path":      path,
		"size":      sent,
		"sha256":    hex.EncodeToString(hasher.Sum(nil)),
		"truncated": truncated,
	}, nil
}

// writeFileTransfer decodes the payload and installs it at p.Path atomically,
// returning the `meta` object of the final `result`: path, bytes_written and
// sha256 of the written bytes. Nothing is touched on disk until the payload
// has been validated, and a failure at any point leaves a pre-existing target
// exactly as it was.
func writeFileTransfer(p WriteFileParams) (map[string]any, error) {
	path, err := transferPath(p.Path)
	if err != nil {
		return nil, err
	}

	// Decode and size-check before touching the disk at all: a malformed or
	// oversized payload must not even create a temporary file.
	content, err := base64.StdEncoding.DecodeString(p.ContentBase64)
	if err != nil {
		return nil, &fileError{code: errCodeInvalidBase64, err: err}
	}
	if len(content) > maxFileTransferBytes {
		return nil, fileErrorf(errCodeFileTooLarge, "%d octets, maximum %d", len(content), maxFileTransferBytes)
	}

	overwrite := p.Overwrite == nil || *p.Overwrite
	existing, statErr := os.Stat(path)
	switch {
	case statErr == nil && existing.IsDir():
		return nil, fileErrorf(errCodeIsADirectory, "%s est un répertoire", path)
	case statErr == nil && !overwrite:
		return nil, fileErrorf(errCodeFileExists, "%s existe déjà (overwrite=false)", path)
	}

	dir := filepath.Dir(path)
	if p.CreateDirs {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}

	// Atomic install: write to a temporary file in the SAME directory (so
	// the rename never crosses a filesystem boundary and is a genuine atomic
	// swap) and only then move it into place. Any failure before the rename
	// leaves the previous content intact, and the deferred cleanup makes
	// sure no temporary file is left behind either.
	tmp, err := os.CreateTemp(dir, ".claude-distant-*.part")
	if err != nil {
		return nil, err
	}
	tmpPath := tmp.Name()
	installed := false
	defer func() {
		if !installed {
			_ = tmp.Close()
			_ = os.Remove(tmpPath)
		}
	}()

	if _, err := tmp.Write(content); err != nil {
		return nil, err
	}
	// Flush to the device before the rename so a crash cannot leave the
	// target pointing at an empty inode.
	if err := tmp.Sync(); err != nil {
		return nil, err
	}
	if err := tmp.Close(); err != nil {
		return nil, err
	}

	// Permissions are a best-effort refinement, applied to the temporary
	// file so the target is never briefly visible with the wrong mode. An
	// unparsable `mode`, or a platform where Chmod is a no-op (Windows),
	// must never fail an otherwise good transfer.
	if perm, ok := parseFileMode(p.Mode); ok {
		_ = os.Chmod(tmpPath, perm)
	} else if statErr == nil {
		// No usable mode given: keep the replaced file's own permissions
		// rather than leaking os.CreateTemp's restrictive 0600.
		_ = os.Chmod(tmpPath, existing.Mode().Perm())
	} else {
		_ = os.Chmod(tmpPath, 0o644)
	}

	if err := os.Rename(tmpPath, path); err != nil {
		return nil, err
	}
	installed = true

	sum := sha256.Sum256(content)
	return map[string]any{
		"path":          path,
		"bytes_written": int64(len(content)),
		"sha256":        hex.EncodeToString(sum[:]),
	}, nil
}

// parseFileMode reads an octal permission string such as "0644" or "600".
// It reports ok=false for an empty or unparsable value, which the caller
// treats as "no mode requested" rather than as an error.
func parseFileMode(mode string) (fs.FileMode, bool) {
	s := strings.TrimSpace(mode)
	if s == "" {
		return 0, false
	}
	n, err := strconv.ParseUint(s, 8, 32)
	if err != nil {
		return 0, false
	}
	return fs.FileMode(n).Perm(), true
}

// base64DecodedLen returns the exact number of bytes a standard, padded
// base64 string decodes to, without allocating the decoded buffer — used to
// state the payload size in the guard-rail prompt before any decoding.
func base64DecodedLen(s string) int {
	n := len(s)
	for n > 0 && s[n-1] == '=' {
		n--
	}
	return n * 6 / 8
}
