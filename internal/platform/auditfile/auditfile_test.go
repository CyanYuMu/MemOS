package auditfile

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWriteLineAppendsOneLine(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "nested", "audit.jsonl")
	w, err := New(Options{Path: path})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for _, line := range []string{`{"n":1}`, `{"n":2}`} {
		if err := w.WriteLine([]byte(line)); err != nil {
			t.Fatalf("WriteLine: %v", err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("Close again: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	got := strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
	if len(got) != 2 || got[0] != `{"n":1}` || got[1] != `{"n":2}` {
		t.Fatalf("lines = %#v", got)
	}
}

func TestWriteLineAfterCloseFails(t *testing.T) {
	t.Parallel()

	w, err := New(Options{Path: filepath.Join(t.TempDir(), "audit.jsonl")})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if err := w.WriteLine([]byte(`{}`)); err == nil {
		t.Fatal("WriteLine after Close succeeded")
	}
}

func TestNewRejectsUnwritablePath(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	blocker := filepath.Join(dir, "not-a-dir")
	if err := os.WriteFile(blocker, []byte("x"), 0o600); err != nil {
		t.Fatalf("write blocker: %v", err)
	}
	if _, err := New(Options{Path: filepath.Join(blocker, "audit.jsonl")}); err == nil {
		t.Fatal("New succeeded under a regular file")
	}
}
