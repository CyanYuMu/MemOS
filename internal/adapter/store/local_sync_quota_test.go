package store_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// newPendingBatch 建一个合法的待提交批次；分块内容在存储层只是 gzip 字节，
// 这里用固定长度的伪数据代替，测试只关心长度。
func newPendingBatch(t *testing.T, s *store.Store, tenantID, batchID string, manifest map[string]int) {
	t.Helper()
	if err := s.CreateBatch(context.Background(), domain.LocalSyncBatch{
		ID:           batchID,
		TenantID:     tenantID,
		DeviceID:     "dev-1",
		Layers:       []domain.LocalLayer{domain.LocalLayerTrace},
		Manifest:     manifest,
		ManifestHash: "sha256:" + batchID,
		TraceMode:    domain.LocalTraceModeSummary,
		CreatedAt:    time.Now().UTC(),
	}); err != nil {
		t.Fatalf("create batch %s: %v", batchID, err)
	}
}

// TestPutChunkCountsOtherPendingBatchesTowardsQuota 未提交批次的分块同样占磁盘，
// 项目配额必须把“其他待提交批次”算进去，否则连续建批次就能写满共享库。
func TestPutChunkCountsOtherPendingBatchesTowardsQuota(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	const quota = int64(100)
	newPendingBatch(t, s, "cube-q", "batch-a", map[string]int{"trace": 1})
	newPendingBatch(t, s, "cube-q", "batch-b", map[string]int{"trace": 1})

	if _, err := s.PutChunk(ctx, "cube-q", "dev-1", "batch-a", 0, make([]byte, 60), 0, quota, now); err != nil {
		t.Fatalf("first chunk: %v", err)
	}
	// 另一批次再写 60 字节：60 + 60 > 100。
	if _, err := s.PutChunk(ctx, "cube-q", "dev-1", "batch-b", 0, make([]byte, 60), 0, quota, now); !errors.Is(err, domain.ErrQuotaExceeded) {
		t.Fatalf("second batch chunk = %v, want quota exceeded", err)
	}
	// 同一批次内继续追加仍按累计计算：60 + 30 <= 100。
	if _, err := s.PutChunk(ctx, "cube-q", "dev-1", "batch-a", 1, make([]byte, 30), 0, quota, now); err != nil {
		t.Fatalf("same batch chunk: %v", err)
	}
	// 替换已有分块不能让配额重复计数，但变大后仍要拦住。
	if _, err := s.PutChunk(ctx, "cube-q", "dev-1", "batch-a", 0, make([]byte, 60), 0, quota, now); err != nil {
		t.Fatalf("replace equal size: %v", err)
	}
	if _, err := s.PutChunk(ctx, "cube-q", "dev-1", "batch-a", 1, make([]byte, 45), 0, quota, now); !errors.Is(err, domain.ErrQuotaExceeded) {
		t.Fatalf("replace bigger chunk = %v, want quota exceeded", err)
	}
	// 失败的写入不能改动已有分块：仍是 60 + 30。
	bytesUsed, rows, err := s.PendingUsage(ctx, "cube-q")
	if err != nil {
		t.Fatalf("pending usage: %v", err)
	}
	if bytesUsed != 90 || rows != 2 {
		t.Fatalf("pending usage = (%d bytes, %d rows), want (90, 2)", bytesUsed, rows)
	}
}

// TestPutChunkAfterFinishFreesQuota 提交后分块被删除，配额随之释放。
func TestPutChunkAfterFinishFreesQuota(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	const quota = int64(100)
	newPendingBatch(t, s, "cube-f", "batch-a", map[string]int{"trace": 2})
	newPendingBatch(t, s, "cube-f", "batch-b", map[string]int{"trace": 1})

	if _, err := s.PutChunk(ctx, "cube-f", "dev-1", "batch-a", 0, make([]byte, 80), 0, quota, now); err != nil {
		t.Fatalf("chunk a: %v", err)
	}
	if _, err := s.PutChunk(ctx, "cube-f", "dev-1", "batch-b", 0, make([]byte, 40), 0, quota, now); !errors.Is(err, domain.ErrQuotaExceeded) {
		t.Fatalf("chunk b before finish = %v, want quota exceeded", err)
	}
	if err := s.FinishBatch(ctx, "cube-f", "dev-1", "batch-a", domain.LocalCommitResult{
		Accepted: 2,
		Cursor:   now,
	}, now); err != nil {
		t.Fatalf("finish batch: %v", err)
	}
	if _, err := s.PutChunk(ctx, "cube-f", "dev-1", "batch-b", 0, make([]byte, 40), 0, quota, now); err != nil {
		t.Fatalf("chunk b after finish: %v", err)
	}
	bytesUsed, rows, err := s.PendingUsage(ctx, "cube-f")
	if err != nil {
		t.Fatalf("pending usage: %v", err)
	}
	if bytesUsed != 40 || rows != 1 {
		t.Fatalf("pending usage = (%d bytes, %d rows), want (40, 1)", bytesUsed, rows)
	}
}

// TestPendingUsageCountsManifestRows 待提交批次的行数按 manifest 声明统计，
// 这样“先建大批次但不传分块”也会立刻占用行配额。
func TestPendingUsageCountsManifestRows(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	newPendingBatch(t, s, "cube-r", "batch-a", map[string]int{"trace": 3, "policy": 2})
	newPendingBatch(t, s, "cube-r", "batch-b", map[string]int{"world_model": 4})
	if _, err := s.PutChunk(ctx, "cube-r", "dev-1", "batch-a", 0, make([]byte, 20), 0, 0, now); err != nil {
		t.Fatalf("chunk a: %v", err)
	}
	if _, err := s.PutChunk(ctx, "cube-r", "dev-1", "batch-b", 0, make([]byte, 30), 0, 0, now); err != nil {
		t.Fatalf("chunk b: %v", err)
	}
	bytesUsed, rows, err := s.PendingUsage(ctx, "cube-r")
	if err != nil {
		t.Fatalf("pending usage: %v", err)
	}
	if bytesUsed != 50 || rows != 9 {
		t.Fatalf("pending usage = (%d bytes, %d rows), want (50, 9)", bytesUsed, rows)
	}

	if err := s.FinishBatch(ctx, "cube-r", "dev-1", "batch-a", domain.LocalCommitResult{Accepted: 5, Cursor: now}, now); err != nil {
		t.Fatalf("finish batch: %v", err)
	}
	bytesUsed, rows, err = s.PendingUsage(ctx, "cube-r")
	if err != nil {
		t.Fatalf("pending usage after finish: %v", err)
	}
	if bytesUsed != 30 || rows != 4 {
		t.Fatalf("pending usage after finish = (%d bytes, %d rows), want (30, 4)", bytesUsed, rows)
	}
}

// TestCreateBatchWithinQuotaChecksDuplicateFirst 幂等重试先判重：pending 已经
// 计入本次声明时，重发同一 batchId 必须得到 409，而不是配额 413。
func TestCreateBatchWithinQuotaChecksDuplicateFirst(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	batch := domain.LocalSyncBatch{
		ID:           "batch-idem-0001",
		TenantID:     "cube-i",
		DeviceID:     "dev-1",
		Layers:       []domain.LocalLayer{domain.LocalLayerTrace},
		Manifest:     map[string]int{"trace": 2},
		ManifestHash: "sha256:idem",
		TraceMode:    domain.LocalTraceModeSummary,
		CreatedAt:    now,
	}
	if err := s.CreateBatchWithinQuota(ctx, batch, 1<<20, 2); err != nil {
		t.Fatalf("first create: %v", err)
	}
	if err := s.CreateBatchWithinQuota(ctx, batch, 1<<20, 2); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("duplicate create = %v, want conflict", err)
	}
	other := batch
	other.ID = "batch-idem-0002"
	if err := s.CreateBatchWithinQuota(ctx, other, 1<<20, 2); !errors.Is(err, domain.ErrQuotaExceeded) {
		t.Fatalf("second batch = %v, want quota exceeded", err)
	}
}

// TestQuotaCacheInvalidatedByWrites 已用量有 10 秒缓存，任何改动存活行的写入
// 都必须立即失效，否则删除后仍显示占满、新写入后仍显示 0。
func TestQuotaCacheInvalidatedByWrites(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	row := domain.LocalRow{
		Layer:           domain.LocalLayerPolicy,
		SourceID:        "cache-1",
		ContentHash:     "sha256:" + strings.Repeat("a", 64),
		Title:           "cache",
		Summary:         "cache",
		Payload:         map[string]any{"id": "cache-1"},
		PayloadGzip:     []byte(`{"id":"cache-1"}`),
		PayloadBytes:    15,
		SourceUpdatedAt: now,
	}
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	if _, _, err := s.ApplyRows(ctx, "cube-cache", "dev-1", []domain.LocalRow{row}, limits); err != nil {
		t.Fatalf("apply: %v", err)
	}
	first, err := s.Quota(ctx, "cube-cache")
	if err != nil || first.UsedRows != 1 || first.UsedBytes != int64(len(row.PayloadGzip)) {
		t.Fatalf("quota after apply = %+v err=%v", first, err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-cache", PageSize: 10})
	if err != nil || len(memories) != 1 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	if err := s.DeleteMemory(ctx, "cube-cache", memories[0].ID, now); err != nil {
		t.Fatalf("delete: %v", err)
	}
	afterDelete, err := s.Quota(ctx, "cube-cache")
	if err != nil || afterDelete.UsedRows != 0 || afterDelete.UsedBytes != 0 {
		t.Fatalf("quota after delete = %+v err=%v", afterDelete, err)
	}
}
