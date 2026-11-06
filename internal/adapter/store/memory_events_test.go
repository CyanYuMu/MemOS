package store_test

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestMemoryEventsAreImmutableIsolatedAndPageWithoutTimestampLoss(t *testing.T) {
	t.Parallel()
	s := openStore(t)
	ctx := context.Background()
	at := domain.UTCTime{Time: time.Now().UTC()}
	for i := range 63 {
		e := domain.MemoryEvent{ID: fmt.Sprintf("e%d", i), Key: fmt.Sprintf("recall:c%d", i), TenantID: "p1", UserID: "u1", Kind: "recall", Status: "succeeded", CallID: fmt.Sprintf("c%d", i), OccurredAt: at, Total: 1, IDs: []string{"m1"}, Memories: []domain.Memory{{ID: "m1", Content: fmt.Sprintf("original %d", i)}}}
		if err := s.AppendMemoryEvent(ctx, e); err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			e.Memories[0].Content = "must never overwrite original"
			if err := s.AppendMemoryEvent(ctx, e); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := s.AppendMemoryEvent(ctx, domain.MemoryEvent{ID: "foreign", Key: "recall:c0", TenantID: "p2", UserID: "u2", Kind: "recall", OccurredAt: at}); err != nil {
		t.Fatal(err)
	}
	first, err := s.ListMemoryEvents(ctx, "p1", 0, 50, false)
	if err != nil || len(first) != 50 || first[0].Memories[0].Content != "original 0" {
		t.Fatalf("first page: %+v %v", first, err)
	}
	second, err := s.ListMemoryEvents(ctx, "p1", first[49].Sequence, 50, false)
	if err != nil || len(second) != 13 || second[12].ID != "e62" {
		t.Fatalf("second page: %+v %v", second, err)
	}
	latest, err := s.ListMemoryEvents(ctx, "p1", 0, 5, true)
	if err != nil || len(latest) != 5 || latest[0].ID != "e62" {
		t.Fatalf("latest page: %+v %v", latest, err)
	}
	foreign, err := s.ListMemoryEvents(ctx, "p2", 0, 50, false)
	if err != nil || len(foreign) != 1 || foreign[0].ID != "foreign" {
		t.Fatalf("scope leaked: %+v %v", foreign, err)
	}
	old, err := s.MemoryEventsByCalls(ctx, "p1", []string{"c0", "c62"})
	if err != nil || len(old) != 2 || old[0].ID != "e62" || old[1].Memories[0].Content != "original 0" {
		t.Fatalf("old lookup lost or rewrote snapshot: %+v %v", old, err)
	}
	foreignLookup, err := s.MemoryEventsByCalls(ctx, "p2", []string{"c0"})
	if err != nil || len(foreignLookup) != 0 {
		t.Fatalf("lookup crossed tenant: %+v %v", foreignLookup, err)
	}
}

func TestMemoryEventsSurviveReopenAndSequenceDoesNotRestart(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "events.db")
	s, err := store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AppendMemoryEvent(ctx, domain.MemoryEvent{ID: "e1", Key: "update:c1", TenantID: "p1", Memories: []domain.Memory{{ID: "m1", Content: "保留原文", Version: 2}}}); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	if err := s.AppendMemoryEvent(ctx, domain.MemoryEvent{ID: "e2", Key: "delete:c2", TenantID: "p1"}); err != nil {
		t.Fatal(err)
	}
	rows, err := s.ListMemoryEvents(ctx, "p1", 0, 50, false)
	if err != nil || len(rows) != 2 || rows[0].Memories[0].Content != "保留原文" || rows[0].Memories[0].Version != 2 || rows[1].Sequence <= rows[0].Sequence {
		t.Fatalf("reopen changed history: %+v %v", rows, err)
	}
}
