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

func TestChatConfigRoundTrip(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "chat.db")
	s, err := store.Open(ctx, path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })

	if _, err := s.GetChatConfig(ctx, "p1"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("missing config err = %v, want ErrNotFound", err)
	}

	updated := time.Now().UTC().Truncate(time.Millisecond)
	cfg := domain.ChatConfig{
		TenantID:  "p1",
		BaseURL:   "https://gw.example/v1",
		Model:     "m1",
		APIKey:    "sk-test",
		UpdatedAt: updated,
	}
	if err := s.UpsertChatConfig(ctx, cfg); err != nil {
		t.Fatalf("Upsert: %v", err)
	}
	got, err := s.GetChatConfig(ctx, "p1")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got != cfg {
		t.Fatalf("got = %+v, want %+v", got, cfg)
	}

	cfg.Model = "m2"
	if err := s.UpsertChatConfig(ctx, cfg); err != nil {
		t.Fatalf("Upsert update: %v", err)
	}
	got, err = s.GetChatConfig(ctx, "p1")
	if err != nil || got.Model != "m2" {
		t.Fatalf("after update = %+v, %v", got, err)
	}

	// 重新打开同一库时 0027 迁移应幂等。
	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	s, err = store.Open(ctx, path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	if got, err := s.GetChatConfig(ctx, "p1"); err != nil || got.Model != "m2" {
		t.Fatalf("after reopen = %+v, %v", got, err)
	}
}
