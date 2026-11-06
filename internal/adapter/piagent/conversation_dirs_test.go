package piagent

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestConversationHistoriesAndQueuesDoNotShareFiles(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	spec := domain.ChatSessionSpec{TenantID: "p", AccountID: "a"}
	old := sessionDirs(root, "p", "a")
	legacy := dirsForSpec(root, spec)
	if old != legacy {
		t.Fatal("legacy path changed")
	}
	seen := map[string]bool{}
	for _, id := range []string{"", "first", "second"} {
		spec.ConversationID = id
		dirs := dirsForSpec(root, spec)
		if seen[dirs.runtime] {
			t.Fatal("shared runtime directory")
		}
		seen[dirs.runtime] = true
		if err := os.MkdirAll(dirs.runtime, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dirs.runtime, "history.json"), []byte(id), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"", "first", "second"} {
		spec.ConversationID = id
		raw, err := os.ReadFile(filepath.Join(dirsForSpec(root, spec).runtime, "history.json"))
		if err != nil || string(raw) != id {
			t.Fatalf("cross-conversation history: %q %v", raw, err)
		}
	}
}
