package store

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func openAuditStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(context.Background(), filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func oldAuditRecords(start, end int, ts time.Time) []domain.AuditRecord {
	records := make([]domain.AuditRecord, 0, end-start)
	for i := start; i < end; i++ {
		records = append(records, domain.AuditRecord{ID: fmt.Sprintf("old-%d", i), TS: ts, Tool: "add_message"})
	}
	return records
}

// TestPurgeAuditBeforeLoopsBatches 用很小的批次大小确认循环删除，而不是只删一批。
func TestPurgeAuditBeforeLoopsBatches(t *testing.T) {
	t.Parallel()

	s := openAuditStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)

	const oldCount = 7
	if err := s.InsertAudits(ctx, oldAuditRecords(0, oldCount, base)); err != nil {
		t.Fatalf("InsertAudits: %v", err)
	}
	if err := s.InsertAudit(ctx, domain.AuditRecord{
		ID: "new", TS: base.Add(48 * time.Hour), Tool: "add_message",
	}); err != nil {
		t.Fatalf("InsertAudit: %v", err)
	}

	deleted, err := s.purgeAuditBefore(ctx, base.Add(24*time.Hour), 2)
	if err != nil {
		t.Fatalf("purgeAuditBefore: %v", err)
	}
	if deleted != oldCount {
		t.Fatalf("deleted = %d, want %d", deleted, oldCount)
	}
	list, total, err := s.ListAudit(ctx, domain.AuditFilter{})
	if err != nil || total != 1 || list[0].ID != "new" {
		t.Fatalf("remaining = %d, %v, list=%+v", total, err, list)
	}
}

// TestPurgeAuditBeforeDefaultBatch 构造多于一批（5000 行）的数据，验证公开入口
// 会持续删除到清空并返回总数。
func TestPurgeAuditBeforeDefaultBatch(t *testing.T) {
	t.Parallel()

	s := openAuditStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)

	const count = purgeAuditBatchSize + 1200
	const chunk = 2000
	for start := 0; start < count; start += chunk {
		end := min(start+chunk, count)
		if err := s.InsertAudits(ctx, oldAuditRecords(start, end, base)); err != nil {
			t.Fatalf("InsertAudits[%d:%d]: %v", start, end, err)
		}
	}

	deleted, err := s.PurgeAuditBefore(ctx, base.Add(time.Hour))
	if err != nil || deleted != count {
		t.Fatalf("Purge = %d, want %d, err=%v", deleted, count, err)
	}
	_, total, err := s.ListAudit(ctx, domain.AuditFilter{})
	if err != nil || total != 0 {
		t.Fatalf("remaining = %d, err=%v", total, err)
	}
	if err := s.Optimize(ctx); err != nil {
		t.Fatalf("Optimize: %v", err)
	}
}

func TestPurgeAuditBeforeHonorsCanceledContext(t *testing.T) {
	t.Parallel()

	s := openAuditStore(t)
	base := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	if err := s.InsertAudits(context.Background(), oldAuditRecords(0, 3, base)); err != nil {
		t.Fatalf("InsertAudits: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	deleted, err := s.PurgeAuditBefore(ctx, base.Add(time.Hour))
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if deleted != 0 {
		t.Fatalf("deleted = %d, want 0", deleted)
	}
	_, total, err := s.ListAudit(context.Background(), domain.AuditFilter{})
	if err != nil || total != 3 {
		t.Fatalf("remaining = %d, err=%v", total, err)
	}
}
