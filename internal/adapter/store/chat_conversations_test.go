package store_test

import (
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestConversationOwnershipAndStablePaging(t *testing.T) {
	t.Parallel()
	s := openStore(t)
	ctx := t.Context()
	at := time.Now().UTC()
	for i := range 23 {
		c := domain.ChatConversation{ID: fmt.Sprintf("c-%02d", i), TenantID: "p1", AccountID: "alice", Title: "title", CreatedAt: at}
		if err := s.CreateChatConversation(ctx, c); err != nil {
			t.Fatal(err)
		}
	}
	first, total, err := s.ListChatConversations(ctx, "p1", "alice", 0)
	if err != nil || total != 23 || len(first) != 20 || first[0].ID != "c-22" {
		t.Fatalf("page=%v total=%d err=%v", first, total, err)
	}
	next, _, err := s.ListChatConversations(ctx, "p1", "alice", 20)
	if err != nil || len(next) != 3 || next[0].ID != "c-02" {
		t.Fatalf("next=%v err=%v", next, err)
	}
	for _, scope := range [][2]string{{"p2", "alice"}, {"p1", "bob"}} {
		if _, err := s.GetChatConversation(ctx, scope[0], scope[1], first[0].ID); !errors.Is(err, domain.ErrNotFound) {
			t.Fatalf("ownership: %v", err)
		}
		rows, total, err := s.ListChatConversations(ctx, scope[0], scope[1], 0)
		if err != nil || len(rows) != 0 || total != 0 {
			t.Fatalf("leaked list %v %d %v", rows, total, err)
		}
	}
}

func TestConversationArchivePersistsAndRestoresOwnerScoped(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "archive.db")
	ctx := t.Context()
	s, err := store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	c := domain.ChatConversation{ID: "qa", TenantID: "p", AccountID: "a", Title: "original", CreatedAt: time.Now().UTC()}
	if err := s.CreateChatConversation(ctx, c); err != nil {
		t.Fatal(err)
	}
	for _, scope := range [][2]string{{"other", "a"}, {"p", "other"}} {
		if err := s.SetChatConversationArchived(ctx, scope[0], scope[1], c.ID, true); !errors.Is(err, domain.ErrNotFound) {
			t.Fatalf("foreign archive: %v", err)
		}
	}
	if err := s.SetChatConversationArchived(ctx, "p", "a", c.ID, true); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	rows, total, err := s.ListChatConversations(ctx, "p", "a", 0)
	if err != nil || total != 0 || len(rows) != 0 {
		t.Fatalf("archive list: %v %d %v", rows, total, err)
	}
	if _, err := s.GetChatConversation(ctx, "p", "a", c.ID); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("archived selectable: %v", err)
	}
	if err := s.SetChatConversationArchived(ctx, "p", "a", c.ID, false); err != nil {
		t.Fatal(err)
	}
	got, err := s.GetChatConversation(ctx, "p", "a", c.ID)
	if err != nil || got.ID != c.ID || got.Title != c.Title {
		t.Fatalf("restore: %+v %v", got, err)
	}
}
