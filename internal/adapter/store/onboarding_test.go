package store_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestOnboardingPersistenceAndIsolation(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "progress.db")
	s, err := store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	for _, id := range []string{"alice", "bob"} {
		if err := s.CreateAccount(ctx, domain.Account{ID: id, Username: id, CreatedAt: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"one", "two"} {
		if err := s.UpsertTenant(ctx, domain.Tenant{ID: id, UserID: id, Name: id, CreatedAt: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	p, err := s.GetOnboarding(ctx, "alice", "one")
	if err != nil || p.Status != "pending" || p.Step != 0 {
		t.Fatalf("initial: %+v %v", p, err)
	}
	p.Status, p.Step = "active", 3
	p.Chat = &domain.OnboardingCheckpoint{Stage: "logs", First: "owned-conversation"}
	if err := s.SaveOnboarding(ctx, "alice", "one", p); err != nil {
		t.Fatal(err)
	}
	if err := s.SaveOnboarding(ctx, "alice", "one", p); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("stale write: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	p, err = s.GetOnboarding(ctx, "alice", "one")
	if err != nil || p.Step != 3 || p.Revision != 1 || p.Chat == nil || p.Chat.First != "owned-conversation" {
		t.Fatalf("reopen: %+v %v", p, err)
	}
	other, err := s.GetOnboarding(ctx, "alice", "two")
	if err != nil || other.Step != 0 || other.Revision != 1 {
		t.Fatalf("project isolation: %+v %v", other, err)
	}
	other, err = s.GetOnboarding(ctx, "bob", "one")
	if err != nil || other.Status != "pending" || other.Revision != 0 {
		t.Fatalf("account isolation: %+v %v", other, err)
	}
}
