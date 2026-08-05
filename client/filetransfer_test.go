package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// Red-first tests for the file transfer channel (client/filetransfer.go, not
// yet written). The contract they pin down is deliberately small and free of
// WebSocket I/O so the logic stays unit-testable:
//
//	func readFileTransfer(p ReadFileParams, emit func(seq int, data string) error) (meta map[string]any, err error)
//	func writeFileTransfer(p WriteFileParams) (meta map[string]any, err error)
//	func fileErrorCode(err error) string
//
// readFileTransfer hands each 192 KiB slice (fileChunkBytes) to emit as
// base64, with seq starting at 0, and returns the `meta` map carried by the
// final `result` message: path/size/sha256/truncated for a read,
// path/bytes_written/sha256 for a write. fileErrorCode maps a returned error
// to one of the stable protocol codes ("file_not_found", "is_a_directory",
// "file_exists", "file_too_large", "invalid_base64", "invalid_params",
// "permission_denied"), and returns "" for a nil error.

// chunkRecorder collects the file_chunk payloads emitted by readFileTransfer.
type chunkRecorder struct {
	seqs   []int
	chunks []string
	err    error // when non-nil, returned by emit to abort the transfer
}

func (c *chunkRecorder) emit(seq int, data string) error {
	c.seqs = append(c.seqs, seq)
	c.chunks = append(c.chunks, data)
	return c.err
}

// bytes decodes and concatenates every emitted chunk, checking along the way
// that no chunk exceeds the protocol's slice size.
func (c *chunkRecorder) bytes(t *testing.T) []byte {
	t.Helper()
	var out []byte
	for i, chunk := range c.chunks {
		raw, err := base64.StdEncoding.DecodeString(chunk)
		if err != nil {
			t.Fatalf("chunk %d: base64 invalide (%v)", i, err)
		}
		if len(raw) > fileChunkBytes {
			t.Errorf("chunk %d: %d octets, max %d", i, len(raw), fileChunkBytes)
		}
		out = append(out, raw...)
	}
	return out
}

// assertSeqsFromZero checks the seq numbers form 0,1,2,… so the relay can
// detect a gap or a reordering.
func (c *chunkRecorder) assertSeqsFromZero(t *testing.T) {
	t.Helper()
	for i, seq := range c.seqs {
		if seq != i {
			t.Fatalf("seqs = %v, attendu une suite croissante depuis 0", c.seqs)
		}
	}
}

// patternBytes builds deterministic, non-repeating-per-block content so a
// mis-ordered or duplicated chunk cannot go unnoticed.
func patternBytes(n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = byte(i*31 + i/251)
	}
	return b
}

func writeTempFile(t *testing.T, dir, name string, content []byte) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, content, 0o644); err != nil {
		t.Fatalf("préparation du fichier %s: %v", path, err)
	}
	return path
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// metaInt64 reads a numeric meta field whatever concrete numeric type the
// implementation chose to store.
func metaInt64(t *testing.T, meta map[string]any, key string) int64 {
	t.Helper()
	v, ok := meta[key]
	if !ok {
		t.Fatalf("meta[%q] absent (meta = %v)", key, meta)
	}
	switch n := v.(type) {
	case int:
		return int64(n)
	case int64:
		return n
	case float64:
		return int64(n)
	default:
		t.Fatalf("meta[%q] = %#v, type numérique attendu", key, v)
		return 0
	}
}

func metaString(t *testing.T, meta map[string]any, key string) string {
	t.Helper()
	v, ok := meta[key].(string)
	if !ok {
		t.Fatalf("meta[%q] = %#v, chaîne attendue", key, meta[key])
	}
	return v
}

func metaBool(t *testing.T, meta map[string]any, key string) bool {
	t.Helper()
	v, ok := meta[key].(bool)
	if !ok {
		t.Fatalf("meta[%q] = %#v, booléen attendu", key, meta[key])
	}
	return v
}

// --- Lecture ---

func TestReadFileTransfer_SplitsIntoSeveralChunks(t *testing.T) {
	content := patternBytes(fileChunkBytes*2 + 1234)
	path := writeTempFile(t, t.TempDir(), "big.bin", content)

	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}

	wantChunks := 3
	if len(rec.chunks) != wantChunks {
		t.Errorf("%d chunks émis, attendu %d (fileChunkBytes = %d)", len(rec.chunks), wantChunks, fileChunkBytes)
	}
	rec.assertSeqsFromZero(t)

	if got := rec.bytes(t); string(got) != string(content) {
		t.Errorf("contenu reconstitué de %d octets, attendu %d octets identiques", len(got), len(content))
	}
	if got := metaInt64(t, meta, "size"); got != int64(len(content)) {
		t.Errorf("meta.size = %d, want %d", got, len(content))
	}
	if got := metaString(t, meta, "sha256"); got != sha256Hex(content) {
		t.Errorf("meta.sha256 = %q, want %q", got, sha256Hex(content))
	}
	if metaBool(t, meta, "truncated") {
		t.Error("meta.truncated = true, want false (le fichier a été lu en entier)")
	}
	if got := metaString(t, meta, "path"); got != path {
		t.Errorf("meta.path = %q, want %q", got, path)
	}
}

func TestReadFileTransfer_SmallFileFitsInOneChunk(t *testing.T) {
	content := []byte("bonjour\n")
	path := writeTempFile(t, t.TempDir(), "small.txt", content)

	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}
	if len(rec.chunks) != 1 {
		t.Fatalf("%d chunks émis, attendu 1", len(rec.chunks))
	}
	if rec.chunks[0] != base64.StdEncoding.EncodeToString(content) {
		t.Errorf("chunk = %q, want %q (base64 standard avec padding)", rec.chunks[0], base64.StdEncoding.EncodeToString(content))
	}
	if got := metaInt64(t, meta, "size"); got != int64(len(content)) {
		t.Errorf("meta.size = %d, want %d", got, len(content))
	}
}

func TestReadFileTransfer_EmptyFile(t *testing.T) {
	path := writeTempFile(t, t.TempDir(), "empty.txt", nil)

	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}
	if got := rec.bytes(t); len(got) != 0 {
		t.Errorf("%d octets émis, attendu 0", len(got))
	}
	if got := metaInt64(t, meta, "size"); got != 0 {
		t.Errorf("meta.size = %d, want 0", got)
	}
	if metaBool(t, meta, "truncated") {
		t.Error("meta.truncated = true, want false")
	}
}

func TestReadFileTransfer_Offset(t *testing.T) {
	content := patternBytes(fileChunkBytes + 500)
	path := writeTempFile(t, t.TempDir(), "offset.bin", content)

	const offset = 1000
	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path, Offset: offset}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}

	want := content[offset:]
	if got := rec.bytes(t); string(got) != string(want) {
		t.Errorf("contenu de %d octets, attendu les %d octets à partir de l'offset %d", len(got), len(want), offset)
	}
	if got := metaInt64(t, meta, "size"); got != int64(len(want)) {
		t.Errorf("meta.size = %d, want %d (octets réellement renvoyés)", got, len(want))
	}
	if got := metaString(t, meta, "sha256"); got != sha256Hex(want) {
		t.Errorf("meta.sha256 = %q, want %q (hash des octets renvoyés)", got, sha256Hex(want))
	}
	if metaBool(t, meta, "truncated") {
		t.Error("meta.truncated = true, want false (tout le reste du fichier a été lu)")
	}
}

func TestReadFileTransfer_MaxBytesTruncates(t *testing.T) {
	content := patternBytes(fileChunkBytes * 2)
	path := writeTempFile(t, t.TempDir(), "trunc.bin", content)

	const maxBytes = fileChunkBytes + 10
	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path, MaxBytes: maxBytes}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}
	rec.assertSeqsFromZero(t)

	want := content[:maxBytes]
	if got := rec.bytes(t); string(got) != string(want) {
		t.Errorf("contenu de %d octets, attendu les %d premiers octets", len(got), maxBytes)
	}
	if got := metaInt64(t, meta, "size"); got != int64(maxBytes) {
		t.Errorf("meta.size = %d, want %d", got, maxBytes)
	}
	if !metaBool(t, meta, "truncated") {
		t.Error("meta.truncated = false, want true (des octets restent au-delà de max_bytes)")
	}
}

func TestReadFileTransfer_MaxBytesEqualToSizeIsNotTruncated(t *testing.T) {
	content := patternBytes(4096)
	path := writeTempFile(t, t.TempDir(), "exact.bin", content)

	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path, MaxBytes: int64(len(content))}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}
	if got := rec.bytes(t); string(got) != string(content) {
		t.Errorf("contenu de %d octets, attendu %d", len(got), len(content))
	}
	if metaBool(t, meta, "truncated") {
		t.Error("meta.truncated = true, want false (max_bytes couvre exactement le fichier)")
	}
}

func TestReadFileTransfer_MaxBytesLargerThanFileReadsEverything(t *testing.T) {
	content := patternBytes(2048)
	path := writeTempFile(t, t.TempDir(), "small.bin", content)

	rec := &chunkRecorder{}
	meta, err := readFileTransfer(ReadFileParams{Path: path, MaxBytes: 1 << 20}, rec.emit)
	if err != nil {
		t.Fatalf("readFileTransfer: %v", err)
	}
	if got := rec.bytes(t); string(got) != string(content) {
		t.Errorf("contenu de %d octets, attendu %d", len(got), len(content))
	}
	if metaBool(t, meta, "truncated") {
		t.Error("meta.truncated = true, want false")
	}
}

func TestReadFileTransfer_ErrorCodes(t *testing.T) {
	dir := t.TempDir()
	sub := filepath.Join(dir, "adir")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	cases := []struct {
		name     string
		params   ReadFileParams
		wantCode string
	}{
		{"fichier absent", ReadFileParams{Path: filepath.Join(dir, "nope.txt")}, "file_not_found"},
		{"répertoire", ReadFileParams{Path: sub}, "is_a_directory"},
		{"chemin vide", ReadFileParams{Path: ""}, "invalid_params"},
		{"chemin blanc", ReadFileParams{Path: "   "}, "invalid_params"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := &chunkRecorder{}
			_, err := readFileTransfer(c.params, rec.emit)
			if err == nil {
				t.Fatalf("readFileTransfer a réussi, attendu l'erreur %q", c.wantCode)
			}
			if got := fileErrorCode(err); got != c.wantCode {
				t.Errorf("fileErrorCode = %q, want %q (err = %v)", got, c.wantCode, err)
			}
			if len(rec.chunks) != 0 {
				t.Errorf("%d chunks émis avant l'erreur, attendu 0", len(rec.chunks))
			}
		})
	}
}

func TestFileErrorCode_NilErrorHasNoCode(t *testing.T) {
	if got := fileErrorCode(nil); got != "" {
		t.Errorf("fileErrorCode(nil) = %q, want \"\"", got)
	}
}

// A failing emit (connection dropped mid-transfer) must abort the read
// instead of silently reporting a complete transfer.
func TestReadFileTransfer_PropagatesEmitError(t *testing.T) {
	path := writeTempFile(t, t.TempDir(), "x.bin", patternBytes(fileChunkBytes*2))

	rec := &chunkRecorder{err: fmt.Errorf("connexion fermée")}
	if _, err := readFileTransfer(ReadFileParams{Path: path}, rec.emit); err == nil {
		t.Fatal("readFileTransfer a réussi alors que emit échoue, attendu une erreur")
	}
	if len(rec.chunks) != 1 {
		t.Errorf("%d chunks émis, attendu 1 (arrêt dès le premier échec)", len(rec.chunks))
	}
}

// --- Écriture ---

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

func boolPtr(b bool) *bool { return &b }

func TestWriteFileTransfer_Nominal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "out.txt")
	content := []byte("contenu écrit à distance\n")

	meta, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: base64.StdEncoding.EncodeToString(content),
	})
	if err != nil {
		t.Fatalf("writeFileTransfer: %v", err)
	}

	got, readErr := os.ReadFile(path)
	if readErr != nil {
		t.Fatalf("relecture: %v", readErr)
	}
	if string(got) != string(content) {
		t.Errorf("contenu = %q, want %q", got, content)
	}
	if n := metaInt64(t, meta, "bytes_written"); n != int64(len(content)) {
		t.Errorf("meta.bytes_written = %d, want %d", n, len(content))
	}
	if h := metaString(t, meta, "sha256"); h != sha256Hex(content) {
		t.Errorf("meta.sha256 = %q, want %q", h, sha256Hex(content))
	}
	if p := metaString(t, meta, "path"); p != path {
		t.Errorf("meta.path = %q, want %q", p, path)
	}

	// L'écriture atomique passe par un fichier temporaire dans le même
	// répertoire : il ne doit rien en rester.
	assertDirEntries(t, dir, "out.txt")
}

func TestWriteFileTransfer_LargeContentSpansManyChunksWorthOfBytes(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "big.bin")
	content := patternBytes(fileChunkBytes*2 + 7)

	if _, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: base64.StdEncoding.EncodeToString(content),
	}); err != nil {
		t.Fatalf("writeFileTransfer: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("relecture: %v", err)
	}
	if string(got) != string(content) {
		t.Errorf("contenu de %d octets, attendu %d octets identiques", len(got), len(content))
	}
}

func TestWriteFileTransfer_CreateDirs(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a", "b", "c.txt")

	if _, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: b64("hello"),
		CreateDirs:    true,
	}); err != nil {
		t.Fatalf("writeFileTransfer: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("relecture: %v", err)
	}
	if string(got) != "hello" {
		t.Errorf("contenu = %q, want hello", got)
	}
}

func TestWriteFileTransfer_MissingParentWithoutCreateDirs(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "absent", "c.txt")

	if _, err := writeFileTransfer(WriteFileParams{Path: path, ContentBase64: b64("hello")}); err == nil {
		t.Fatal("writeFileTransfer a réussi sans create_dirs alors que le parent n'existe pas")
	}
	if _, err := os.Stat(filepath.Join(dir, "absent")); !os.IsNotExist(err) {
		t.Error("le répertoire parent a été créé alors que create_dirs est false")
	}
}

func TestWriteFileTransfer_OverwriteFalseOnExistingFile(t *testing.T) {
	dir := t.TempDir()
	original := []byte("contenu original\n")
	path := writeTempFile(t, dir, "existing.txt", original)

	_, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: b64("nouveau contenu"),
		Overwrite:     boolPtr(false),
	})
	if err == nil {
		t.Fatal("writeFileTransfer a réussi avec overwrite=false sur un fichier existant")
	}
	if got := fileErrorCode(err); got != "file_exists" {
		t.Errorf("fileErrorCode = %q, want file_exists (err = %v)", got, err)
	}
	assertFileContent(t, path, original)
	assertDirEntries(t, dir, "existing.txt")
}

func TestWriteFileTransfer_OverwriteDefaultsToTrue(t *testing.T) {
	dir := t.TempDir()
	path := writeTempFile(t, dir, "existing.txt", []byte("ancien"))

	// Overwrite nil = champ absent côté harnais = défaut true.
	if _, err := writeFileTransfer(WriteFileParams{Path: path, ContentBase64: b64("nouveau")}); err != nil {
		t.Fatalf("writeFileTransfer: %v", err)
	}
	assertFileContent(t, path, []byte("nouveau"))

	if _, err := writeFileTransfer(WriteFileParams{Path: path, ContentBase64: b64("encore"), Overwrite: boolPtr(true)}); err != nil {
		t.Fatalf("writeFileTransfer (overwrite explicite): %v", err)
	}
	assertFileContent(t, path, []byte("encore"))
}

func TestWriteFileTransfer_InvalidBase64(t *testing.T) {
	dir := t.TempDir()
	original := []byte("intact\n")
	path := writeTempFile(t, dir, "target.txt", original)

	_, err := writeFileTransfer(WriteFileParams{Path: path, ContentBase64: "ceci n'est pas du base64 !!"})
	if err == nil {
		t.Fatal("writeFileTransfer a réussi avec un base64 invalide")
	}
	if got := fileErrorCode(err); got != "invalid_base64" {
		t.Errorf("fileErrorCode = %q, want invalid_base64 (err = %v)", got, err)
	}
	// Atomicité : la cible préexistante est intacte et aucun temporaire
	// ne traîne dans le répertoire.
	assertFileContent(t, path, original)
	assertDirEntries(t, dir, "target.txt")
}

func TestWriteFileTransfer_TooLarge(t *testing.T) {
	dir := t.TempDir()
	original := []byte("intact\n")
	path := writeTempFile(t, dir, "target.txt", original)

	oversized := make([]byte, maxFileTransferBytes+1)
	_, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: base64.StdEncoding.EncodeToString(oversized),
	})
	if err == nil {
		t.Fatal("writeFileTransfer a réussi au-delà de maxFileTransferBytes")
	}
	if got := fileErrorCode(err); got != "file_too_large" {
		t.Errorf("fileErrorCode = %q, want file_too_large (err = %v)", got, err)
	}
	assertFileContent(t, path, original)
	assertDirEntries(t, dir, "target.txt")
}

func TestWriteFileTransfer_ExactlyMaxSizeIsAccepted(t *testing.T) {
	if testing.Short() {
		t.Skip("écrit 8 MiB sur disque")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "max.bin")

	content := make([]byte, maxFileTransferBytes)
	if _, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: base64.StdEncoding.EncodeToString(content),
	}); err != nil {
		t.Fatalf("writeFileTransfer à la limite exacte: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if info.Size() != int64(maxFileTransferBytes) {
		t.Errorf("taille = %d, want %d", info.Size(), maxFileTransferBytes)
	}
}

func TestWriteFileTransfer_EmptyPath(t *testing.T) {
	for _, p := range []string{"", "   "} {
		_, err := writeFileTransfer(WriteFileParams{Path: p, ContentBase64: b64("x")})
		if err == nil {
			t.Fatalf("path %q: writeFileTransfer a réussi, attendu invalid_params", p)
		}
		if got := fileErrorCode(err); got != "invalid_params" {
			t.Errorf("path %q: fileErrorCode = %q, want invalid_params", p, got)
		}
	}
}

func TestWriteFileTransfer_TargetIsDirectory(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "adir")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	_, err := writeFileTransfer(WriteFileParams{Path: target, ContentBase64: b64("x")})
	if err == nil {
		t.Fatal("writeFileTransfer a réussi sur un chemin qui est un répertoire")
	}
	if got := fileErrorCode(err); got != "is_a_directory" {
		t.Errorf("fileErrorCode = %q, want is_a_directory (err = %v)", got, err)
	}
	if info, statErr := os.Stat(target); statErr != nil || !info.IsDir() {
		t.Error("le répertoire cible n'est plus intact après l'échec")
	}
}

func TestWriteFileTransfer_AppliesMode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("os.Chmod est largement no-op sur Windows")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "secret.txt")

	if _, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: b64("s3cr3t"),
		Mode:          "0600",
	}); err != nil {
		t.Fatalf("writeFileTransfer: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("permissions = %04o, want 0600", perm)
	}
}

// Un `mode` illisible ne doit pas faire échouer le transfert : le contenu
// prime, les permissions sont un raffinement best-effort (cf. Windows).
func TestWriteFileTransfer_UnparsableModeDoesNotFailTransfer(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "out.txt")

	if _, err := writeFileTransfer(WriteFileParams{
		Path:          path,
		ContentBase64: b64("data"),
		Mode:          "rw-r--r--",
	}); err != nil {
		t.Fatalf("writeFileTransfer: %v", err)
	}
	assertFileContent(t, path, []byte("data"))
}

// Atomicité : quand l'écriture échoue en cours de route (ici parce que le
// répertoire cible est en lecture seule, donc ni le temporaire ni le rename
// ne peuvent aboutir), le fichier cible préexistant doit rester intact.
func TestWriteFileTransfer_AtomicityKeepsExistingFileIntactOnFailure(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("les permissions de répertoire POSIX ne s'appliquent pas de la même façon")
	}
	if os.Geteuid() == 0 {
		t.Skip("root ignore les permissions du répertoire")
	}

	dir := t.TempDir()
	original := []byte("contenu original à préserver\n")
	path := writeTempFile(t, dir, "target.txt", original)

	if err := os.Chmod(dir, 0o500); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o755) })

	if _, err := writeFileTransfer(WriteFileParams{Path: path, ContentBase64: b64("contenu de remplacement")}); err == nil {
		t.Fatal("writeFileTransfer a réussi dans un répertoire en lecture seule")
	}

	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	assertFileContent(t, path, original)
	assertDirEntries(t, dir, "target.txt")
}

func assertFileContent(t *testing.T, path string, want []byte) {
	t.Helper()
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("relecture de %s: %v", path, err)
	}
	if string(got) != string(want) {
		t.Errorf("contenu de %s = %q, want %q", path, got, want)
	}
}

// assertDirEntries vérifie qu'il ne reste dans dir que les entrées attendues
// — autrement dit qu'aucun fichier temporaire d'écriture atomique n'a été
// abandonné.
func assertDirEntries(t *testing.T, dir string, want ...string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("lecture de %s: %v", dir, err)
	}
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if len(names) != len(want) {
		t.Errorf("entrées de %s = %v, want %v (résidu de fichier temporaire ?)", dir, names, want)
		return
	}
	for _, w := range want {
		found := false
		for _, n := range names {
			if n == w {
				found = true
			}
		}
		if !found {
			t.Errorf("entrées de %s = %v, want %v", dir, names, want)
			return
		}
	}
}
