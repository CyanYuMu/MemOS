package store_test

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// TestApplyRowsRevivedRowCountsAgainstQuota 软删除的行不占配额，但同一
// source_id 再次上传会把它复活；复活必须重新计入行数，否则反复“删除→上传”
// 可以无限突破行数上限。
func TestApplyRowsRevivedRowCountsAgainstQuota(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 1, UsedBytes: 0}

	row := func(layer domain.LocalLayer, id, hash string) domain.LocalRow {
		return domain.LocalRow{
			Layer:           layer,
			SourceID:        id,
			ContentHash:     "sha256:" + hash,
			Title:           id,
			Summary:         id,
			Payload:         map[string]any{"id": id},
			PayloadGzip:     []byte(`{"id":"` + id + `"}`),
			PayloadBytes:    12,
			SourceUpdatedAt: now,
		}
	}
	if _, _, err := s.ApplyRows(ctx, "cube-a", "dev-1", []domain.LocalRow{row(domain.LocalLayerPolicy, "p1", "a")}, limits); err != nil {
		t.Fatalf("apply first: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-a", PageSize: 10})
	if err != nil || len(memories) != 1 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	if err := s.DeleteMemory(ctx, "cube-a", memories[0].ID, now); err != nil {
		t.Fatalf("delete: %v", err)
	}
	// 删除后额度释放，另一行可以写入。
	if _, _, err := s.ApplyRows(ctx, "cube-a", "dev-1", []domain.LocalRow{row(domain.LocalLayerPolicy, "p2", "b")}, limits); err != nil {
		t.Fatalf("apply second: %v", err)
	}
	// 复活 p1：内容变了才会走更新分支，此时行数应为 2，超过上限 1。
	_, _, err = s.ApplyRows(ctx, "cube-a", "dev-1", []domain.LocalRow{row(domain.LocalLayerPolicy, "p1", "c")}, limits)
	if !errors.Is(err, domain.ErrQuotaExceeded) {
		t.Fatalf("revive p1 = %v, want quota exceeded", err)
	}
}

// TestPurgeMirroredBeforeDropsOnlyOldRows 保留期按 synced_at（最近一次同步时间）
// 清理，早于截止点的行被删除，晚于截止点的行保留。
func TestPurgeMirroredBeforeDropsOnlyOldRows(t *testing.T) {
	t.Parallel()

	s, path := openStoreWithPath(t)
	ctx := context.Background()
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	const insert = `INSERT INTO local_memories
		(id, tenant_id, device_id, layer, source_id, content_hash, payload, source_updated_at, synced_at)
		VALUES (?, 'cube-a', 'dev-1', 'trace', ?, 'sha256:x', ?, ?, ?)`
	for _, row := range []struct{ id, syncedAt string }{
		{"old", "2026-01-01T00:00:00.000Z"},
		{"new", "2026-10-01T00:00:00.000Z"},
	} {
		if _, err := db.ExecContext(ctx, insert, row.id, row.id, []byte("x"), row.syncedAt, row.syncedAt); err != nil {
			t.Fatalf("insert %s: %v", row.id, err)
		}
	}

	cutoff := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	purged, err := s.PurgeMirroredBefore(ctx, cutoff)
	if err != nil {
		t.Fatalf("PurgeMirroredBefore: %v", err)
	}
	if purged != 1 {
		t.Fatalf("purged = %d, want 1", purged)
	}
	var remaining int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM local_memories`).Scan(&remaining); err != nil {
		t.Fatalf("count: %v", err)
	}
	if remaining != 1 {
		t.Fatalf("remaining = %d, want 1", remaining)
	}
}

// TestFinishBatchDropsChunks 提交成功后分块必须删掉：分块里是客户端上传的原始
// JSONL，summary 模式下被拒的原始正文也在其中；只清 pending 批次会让它长期留在
// 控制面库里，绕过“拒绝原始正文”的隐私承诺。
func TestFinishBatchDropsChunks(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()

	batch := domain.LocalSyncBatch{
		ID:        "batch-1",
		TenantID:  "cube-a",
		DeviceID:  "dev-1",
		Layers:    []domain.LocalLayer{domain.LocalLayerTrace},
		Manifest:  map[string]int{"trace": 1},
		TraceMode: domain.LocalTraceModeSummary,
		CreatedAt: now,
	}
	if err := s.CreateBatch(ctx, batch); err != nil {
		t.Fatalf("CreateBatch: %v", err)
	}
	if _, err := s.PutChunk(ctx, "cube-a", "dev-1", "batch-1", 0, []byte(`{"userText":"raw"}`), 1<<20, 0, now); err != nil {
		t.Fatalf("PutChunk: %v", err)
	}
	if _, err := s.GetChunk(ctx, "cube-a", "dev-1", "batch-1", 0); err != nil {
		t.Fatalf("GetChunk before commit: %v", err)
	}

	if err := s.FinishBatch(ctx, "cube-a", "dev-1", "batch-1",
		domain.LocalCommitResult{Accepted: 1}, now); err != nil {
		t.Fatalf("FinishBatch: %v", err)
	}
	if _, err := s.GetChunk(ctx, "cube-a", "dev-1", "batch-1", 0); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("chunk after commit = %v, want not found", err)
	}
}

// TestApplyRowsReviveAddsOnlyNewBytes 复活软删除行时，旧长度本来就不在 usedBytes 里，
// 只能加新长度；否则会出现负用量并绕过字节配额。
func TestApplyRowsReviveAddsOnlyNewBytes(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}

	big := domain.LocalRow{
		Layer: domain.LocalLayerPolicy, SourceID: "p1", ContentHash: "sha256:a",
		Title: "p1", Summary: "p1",
		Payload:         map[string]any{"text": strings.Repeat("a", 200)},
		PayloadGzip:     []byte(strings.Repeat("a", 200)),
		PayloadBytes:    200,
		SourceUpdatedAt: now,
	}
	if _, _, err := s.ApplyRows(ctx, "cube-a", "dev-1", []domain.LocalRow{big}, limits); err != nil {
		t.Fatalf("apply big: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-a", PageSize: 10})
	if err != nil || len(memories) != 1 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	if err := s.DeleteMemory(ctx, "cube-a", memories[0].ID, now); err != nil {
		t.Fatalf("delete: %v", err)
	}

	small := big
	small.ContentHash = "sha256:b"
	small.PayloadGzip = []byte("0123456789")
	small.PayloadBytes = 10
	if _, _, err := s.ApplyRows(ctx, "cube-a", "dev-1", []domain.LocalRow{small}, limits); err != nil {
		t.Fatalf("revive: %v", err)
	}
	quota, err := s.Quota(ctx, "cube-a")
	if err != nil {
		t.Fatalf("quota: %v", err)
	}
	if quota.UsedBytes != 10 {
		t.Fatalf("usedBytes = %d, want 10 (revive must not subtract the old length)", quota.UsedBytes)
	}
	if quota.UsedRows != 1 {
		t.Fatalf("usedRows = %d, want 1", quota.UsedRows)
	}
}

// TestPurgeMirroredBeforeKeepsActivePromotions 保留期清理不能删仍在排队/执行中的转存行：
// 作业完成时会更新 0 行，云端却已经写入，留下没有本地来源的记忆。
func TestPurgeMirroredBeforeKeepsActivePromotions(t *testing.T) {
	t.Parallel()

	s, path := openStoreWithPath(t)
	ctx := context.Background()
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	const insert = `INSERT INTO local_memories
		(id, tenant_id, device_id, layer, source_id, content_hash, payload, source_updated_at, synced_at, promote_status)
		VALUES (?, 'cube-a', 'dev-1', 'trace', ?, 'sha256:x', ?, ?, ?, ?)`
	old := "2026-01-01T00:00:00.000Z"
	for _, row := range []struct{ id, status string }{
		{"done", ""},
		{"queued", domain.LocalPromoteQueued},
		{"running", domain.LocalPromoteRunning},
	} {
		if _, err := db.ExecContext(ctx, insert, row.id, row.id, []byte("x"), old, old, row.status); err != nil {
			t.Fatalf("insert %s: %v", row.id, err)
		}
	}

	purged, err := s.PurgeMirroredBefore(ctx, time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatalf("PurgeMirroredBefore: %v", err)
	}
	if purged != 1 {
		t.Fatalf("purged = %d, want 1", purged)
	}
	rows, err := db.QueryContext(ctx, `SELECT id FROM local_memories ORDER BY id`)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	defer func() { _ = rows.Close() }()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			t.Fatalf("scan: %v", err)
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("rows: %v", err)
	}
	if strings.Join(ids, ",") != "queued,running" {
		t.Fatalf("remaining = %v, want queued,running", ids)
	}
}

// TestPurgeDeletedBeforePhysicallyRemovesOldRows 软删除立即释放配额，但行仍占
// 磁盘；超过清理期限后必须真正 DELETE，否则“删除→再上传”可以无限撑大共享库。
func TestPurgeDeletedBeforePhysicallyRemovesOldRows(t *testing.T) {
	t.Parallel()

	s, path := openStoreWithPath(t)
	ctx := context.Background()
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	const insert = `INSERT INTO local_memories
		(id, tenant_id, device_id, layer, source_id, content_hash, payload, source_updated_at, synced_at, deleted_at)
		VALUES (?, 'cube-a', 'dev-1', 'trace', ?, 'sha256:x', ?, ?, ?, ?)`
	for _, row := range []struct{ id, deletedAt string }{
		{"old", "2026-01-01T00:00:00.000Z"},
		{"new", "2026-10-01T00:00:00.000Z"},
	} {
		if _, err := db.ExecContext(ctx, insert, row.id, row.id, []byte("x"),
			"2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z", row.deletedAt); err != nil {
			t.Fatalf("insert %s: %v", row.id, err)
		}
	}
	// 未删除的行不能被这条清理动到。
	if _, err := db.ExecContext(ctx, insert, "live", "live", []byte("x"),
		"2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z", ""); err != nil {
		t.Fatalf("insert live: %v", err)
	}

	cutoff := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	purged, err := s.PurgeDeletedBefore(ctx, cutoff)
	if err != nil {
		t.Fatalf("PurgeDeletedBefore: %v", err)
	}
	if purged != 1 {
		t.Fatalf("purged = %d, want 1", purged)
	}
	var remaining int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM local_memories`).Scan(&remaining); err != nil {
		t.Fatalf("count: %v", err)
	}
	if remaining != 2 {
		t.Fatalf("remaining = %d, want 2 (new + live)", remaining)
	}
}

// TestTimestampOnlyRefreshDoesNotTouchQuotaOrTombstone 只有时间戳变化的重复上传：
// 不重写正文、不重复计配额，但必须推进 source_updated_at（/state 游标来自它），
// 并且不能顺手删掉内容不同的旧墓碑。
func TestTimestampOnlyRefreshDoesNotTouchQuotaOrTombstone(t *testing.T) {
	t.Parallel()

	s, path := openStoreWithPath(t)
	ctx := context.Background()
	db := openRawDB(t, path)
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	row := func(hashChar string, updated time.Time) domain.LocalRow {
		return domain.LocalRow{
			Layer:           domain.LocalLayerPolicy,
			SourceID:        "refresh-1",
			ContentHash:     "sha256:" + strings.Repeat(hashChar, 64),
			ContentDigest:   "sha256:" + strings.Repeat("d", 64),
			Title:           "refresh",
			Summary:         "refresh",
			Payload:         map[string]any{"id": "refresh-1"},
			PayloadGzip:     []byte(`{"id":"refresh-1"}`),
			PayloadBytes:    15,
			SourceUpdatedAt: updated,
		}
	}
	if _, _, err := s.ApplyRows(ctx, "cube-ref", "dev-1", []domain.LocalRow{row("a", now)}, limits); err != nil {
		t.Fatalf("apply: %v", err)
	}
	before, err := s.Quota(ctx, "cube-ref")
	if err != nil {
		t.Fatalf("quota: %v", err)
	}
	// 先放一条内容不同的旧墓碑：刷新路径不能删它。
	if _, err := db.ExecContext(ctx, `
		INSERT INTO local_memory_tombstones
			(tenant_id, layer, source_id, device_id, content_hash, content_digest, deleted_at)
		VALUES ('cube-ref', 'policy', 'refresh-1', 'dev-1', ?, ?, ?)`,
		"sha256:"+strings.Repeat("0", 64), "sha256:"+strings.Repeat("0", 64),
		testTimeText(now.Add(-time.Hour))); err != nil {
		t.Fatalf("insert tombstone: %v", err)
	}

	refreshedAt := now.Add(time.Minute)
	applied, unchanged, err := s.ApplyRows(ctx, "cube-ref", "dev-1", []domain.LocalRow{row("b", refreshedAt)}, limits)
	if err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("refresh = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
	after, err := s.Quota(ctx, "cube-ref")
	if err != nil {
		t.Fatalf("quota after: %v", err)
	}
	if after.UsedBytes != before.UsedBytes || after.UsedRows != before.UsedRows {
		t.Fatalf("quota after refresh = %+v, want %+v", after, before)
	}
	var updatedAt, storedHash string
	if err := db.QueryRowContext(ctx, `
		SELECT source_updated_at, content_hash FROM local_memories
		WHERE tenant_id = 'cube-ref' AND source_id = 'refresh-1'`).Scan(&updatedAt, &storedHash); err != nil {
		t.Fatalf("read row: %v", err)
	}
	if updatedAt != testTimeText(refreshedAt) {
		t.Fatalf("source_updated_at = %q, want %q", updatedAt, testTimeText(refreshedAt))
	}
	if storedHash != "sha256:"+strings.Repeat("a", 64) {
		t.Fatalf("content_hash rewritten = %q", storedHash)
	}
	var tombstones int
	if err := db.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM local_memory_tombstones
		WHERE tenant_id = 'cube-ref' AND source_id = 'refresh-1'`).Scan(&tombstones); err != nil {
		t.Fatalf("count tombstones: %v", err)
	}
	if tombstones != 1 {
		t.Fatalf("tombstones = %d, want 1 (refresh must not drop it)", tombstones)
	}
}

// TestTombstoneMatchesDigestAcrossTimestampRefresh 墓碑优先按 contentDigest 比对：
// 插件因 embedding 重试只刷新 updated_at 时，contentHash 变了但内容没变，不能把
// 已删除的记忆当成新内容复活。
func TestTombstoneMatchesDigestAcrossTimestampRefresh(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	row := func(hashChar string, updatedAt time.Time) domain.LocalRow {
		return domain.LocalRow{
			Layer:           domain.LocalLayerPolicy,
			SourceID:        "digest-1",
			ContentHash:     "sha256:" + strings.Repeat(hashChar, 64),
			ContentDigest:   "sha256:" + strings.Repeat("c", 64),
			Title:           "digest",
			Summary:         "digest",
			Payload:         map[string]any{"id": "digest-1"},
			PayloadGzip:     []byte(`{"id":"digest-1"}`),
			PayloadBytes:    15,
			SourceUpdatedAt: updatedAt,
		}
	}
	if _, _, err := s.ApplyRows(ctx, "cube-d", "dev-1", []domain.LocalRow{row("a", now)}, limits); err != nil {
		t.Fatalf("apply: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-d", PageSize: 10})
	if err != nil || len(memories) != 1 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	if err := s.DeleteMemory(ctx, "cube-d", memories[0].ID, now); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := s.PurgeDeletedBefore(ctx, now.Add(time.Hour)); err != nil {
		t.Fatalf("purge: %v", err)
	}

	// 同一内容、更新的时间戳（hash 变、digest 不变）：不得复活。
	applied, unchanged, err := s.ApplyRows(ctx, "cube-d", "dev-2",
		[]domain.LocalRow{row("b", now.Add(time.Minute))}, limits)
	if err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("resend refreshed row = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
	if memories, _, err = s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-d", PageSize: 10}); err != nil || len(memories) != 0 {
		t.Fatalf("revived by timestamp refresh: %v len=%d", err, len(memories))
	}

	// 内容真变了（digest 变）才允许重新出现。
	changed := row("d", now.Add(2*time.Minute))
	changed.ContentDigest = "sha256:" + strings.Repeat("e", 64)
	if applied, _, err = s.ApplyRows(ctx, "cube-d", "dev-2", []domain.LocalRow{changed}, limits); err != nil || applied != 1 {
		t.Fatalf("re-apply changed digest = (%d,%v), want (1,nil)", applied, err)
	}
}

// TestPurgeMirroredBeforeKeepsDeletedRowsTombstoned 镜像保留期清理已软删除的行时
// 必须同时留墓碑；否则保留期一到，删除标记被“洗掉”，同内容重传就会复活。
func TestPurgeMirroredBeforeKeepsDeletedRowsTombstoned(t *testing.T) {
	t.Parallel()

	s, path := openStoreWithPath(t)
	ctx := context.Background()
	db := openRawDB(t, path)
	old := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	mkRow := func(id string) domain.LocalRow {
		return domain.LocalRow{
			Layer:           domain.LocalLayerPolicy,
			SourceID:        id,
			ContentHash:     "sha256:" + strings.Repeat("a", 64),
			ContentDigest:   "sha256:" + strings.Repeat("b", 64),
			Title:           id,
			Summary:         id,
			Payload:         map[string]any{"id": id},
			PayloadGzip:     []byte(`{"id":"` + id + `"}`),
			PayloadBytes:    15,
			SourceUpdatedAt: old,
		}
	}
	if _, _, err := s.ApplyRows(ctx, "cube-m", "dev-1", []domain.LocalRow{mkRow("deleted-1"), mkRow("live-1")}, limits); err != nil {
		t.Fatalf("apply: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-m", PageSize: 10})
	if err != nil || len(memories) != 2 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	for _, m := range memories {
		if m.SourceID == "deleted-1" {
			if err := s.DeleteMemory(ctx, "cube-m", m.ID, old); err != nil {
				t.Fatalf("delete: %v", err)
			}
		}
	}

	// 把 synced_at 调到保留期之前，再按保留期清理。
	if _, err := db.ExecContext(ctx,
		`UPDATE local_memories SET synced_at = ? WHERE tenant_id = 'cube-m'`, testTimeText(old)); err != nil {
		t.Fatalf("backdate synced_at: %v", err)
	}
	if _, err := s.PurgeMirroredBefore(ctx, old.Add(time.Hour)); err != nil {
		t.Fatalf("purge mirrors: %v", err)
	}

	// 已删除的那份不能再回来。
	applied, unchanged, err := s.ApplyRows(ctx, "cube-m", "dev-1", []domain.LocalRow{mkRow("deleted-1")}, limits)
	if err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("resend deleted row = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
	// 仅超期的存活行没有墓碑：重新同步应当恢复。
	if applied, _, err = s.ApplyRows(ctx, "cube-m", "dev-1", []domain.LocalRow{mkRow("live-1")}, limits); err != nil || applied != 1 {
		t.Fatalf("resync live row = (%d,%v), want (1,nil)", applied, err)
	}
}

// TestFreshTombstoneSurvivesImmediateTombstonePurge 墓碑时间取物理清理时刻：
// 若沿用用户原始删除时间，删除保留期比墓碑保留期长时，新建墓碑会在同一轮维护
// 立刻被清掉，已删除内容就能重传复活。
func TestFreshTombstoneSurvivesImmediateTombstonePurge(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	old := now.Add(-30 * 24 * time.Hour)
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	row := domain.LocalRow{
		Layer:           domain.LocalLayerPolicy,
		SourceID:        "fresh-tomb",
		ContentHash:     "sha256:" + strings.Repeat("a", 64),
		ContentDigest:   "sha256:" + strings.Repeat("b", 64),
		Title:           "fresh",
		Summary:         "fresh",
		Payload:         map[string]any{"id": "fresh-tomb"},
		PayloadGzip:     []byte(`{"id":"fresh-tomb"}`),
		PayloadBytes:    18,
		SourceUpdatedAt: old,
	}
	if _, _, err := s.ApplyRows(ctx, "cube-fresh", "dev-1", []domain.LocalRow{row}, limits); err != nil {
		t.Fatalf("apply: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-fresh", PageSize: 10})
	if err != nil || len(memories) != 1 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	// 用户在一个月前删除，物理清理发生在现在。
	if err := s.DeleteMemory(ctx, "cube-fresh", memories[0].ID, old); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := s.PurgeDeletedBefore(ctx, now); err != nil {
		t.Fatalf("purge deleted: %v", err)
	}
	// 墓碑保留期 1 小时：刚创建的墓碑不能在这一轮被清掉。
	purged, err := s.PurgeTombstonesBefore(ctx, now.Add(-time.Hour))
	if err != nil {
		t.Fatalf("purge tombstones: %v", err)
	}
	if purged != 0 {
		t.Fatalf("purged = %d, want 0 (fresh tombstone)", purged)
	}
	if applied, unchanged, err := s.ApplyRows(ctx, "cube-fresh", "dev-2", []domain.LocalRow{row}, limits); err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("resend = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
}

// TestTombstoneConflictKeepsNewestDeletion 同一 source_id 在多个设备上有异构
// 内容时，墓碑只能存一行：必须保留较新的那次删除，不能让旧内容把它覆盖。
func TestTombstoneConflictKeepsNewestDeletion(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	row := func(char string, updated time.Time) domain.LocalRow {
		return domain.LocalRow{
			Layer:           domain.LocalLayerPolicy,
			SourceID:        "shared-1",
			ContentHash:     "sha256:" + strings.Repeat(char, 64),
			ContentDigest:   "sha256:" + strings.Repeat(char, 64),
			Title:           char,
			Summary:         char,
			Payload:         map[string]any{"id": "shared-1"},
			PayloadGzip:     []byte(`{"id":"shared-1"}`),
			PayloadBytes:    18,
			SourceUpdatedAt: updated,
		}
	}
	if _, _, err := s.ApplyRows(ctx, "cube-conf", "dev-1", []domain.LocalRow{row("a", now)}, limits); err != nil {
		t.Fatalf("apply dev-1: %v", err)
	}
	if _, _, err := s.ApplyRows(ctx, "cube-conf", "dev-2", []domain.LocalRow{row("b", now)}, limits); err != nil {
		t.Fatalf("apply dev-2: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-conf", PageSize: 10})
	if err != nil || len(memories) != 2 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	for _, m := range memories {
		deletedAt := now.Add(-time.Hour)
		if m.DeviceID == "dev-2" {
			deletedAt = now
		}
		if err := s.DeleteMemory(ctx, "cube-conf", m.ID, deletedAt); err != nil {
			t.Fatalf("delete %s: %v", m.DeviceID, err)
		}
	}
	if _, err := s.PurgeDeletedBefore(ctx, now.Add(time.Hour)); err != nil {
		t.Fatalf("purge: %v", err)
	}
	// 较新的删除（dev-2 的内容）仍然被挡住。
	if applied, unchanged, err := s.ApplyRows(ctx, "cube-conf", "dev-2",
		[]domain.LocalRow{row("b", now.Add(time.Minute))}, limits); err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("newest content = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
	// 较旧的那次删除被较新的墓碑覆盖，允许重新出现。
	if applied, _, err := s.ApplyRows(ctx, "cube-conf", "dev-1",
		[]domain.LocalRow{row("a", now.Add(2*time.Minute))}, limits); err != nil || applied != 1 {
		t.Fatalf("older content = (%d,%v), want (1,nil)", applied, err)
	}
}

// TestPurgeTombstonesBeforeKeepsRecent 墓碑保留期只清理超过期限的墓碑，近期墓碑
// 仍要挡住重传。
func TestPurgeTombstonesBeforeKeepsRecent(t *testing.T) {
	t.Parallel()

	s, path := openStoreWithPath(t)
	ctx := context.Background()
	db := openRawDB(t, path)
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	mkRow := func(id string) domain.LocalRow {
		return domain.LocalRow{
			Layer:           domain.LocalLayerPolicy,
			SourceID:        id,
			ContentHash:     "sha256:" + strings.Repeat("a", 64),
			Title:           id,
			Summary:         id,
			Payload:         map[string]any{"id": id},
			PayloadGzip:     []byte(`{"id":"` + id + `"}`),
			PayloadBytes:    15,
			SourceUpdatedAt: now,
		}
	}
	for _, id := range []string{"old-1", "new-1"} {
		if _, _, err := s.ApplyRows(ctx, "cube-p", "dev-1", []domain.LocalRow{mkRow(id)}, limits); err != nil {
			t.Fatalf("apply %s: %v", id, err)
		}
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-p", PageSize: 10})
	if err != nil || len(memories) != 2 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	for _, m := range memories {
		if err := s.DeleteMemory(ctx, "cube-p", m.ID, now); err != nil {
			t.Fatalf("delete %s: %v", m.SourceID, err)
		}
	}
	if _, err := s.PurgeDeletedBefore(ctx, now.Add(time.Hour)); err != nil {
		t.Fatalf("purge rows: %v", err)
	}
	if _, err := db.ExecContext(ctx,
		`UPDATE local_memory_tombstones SET deleted_at = ? WHERE source_id = 'old-1'`, testTimeText(now.Add(-48*time.Hour))); err != nil {
		t.Fatalf("backdate tombstone: %v", err)
	}
	purged, err := s.PurgeTombstonesBefore(ctx, now.Add(-24*time.Hour))
	if err != nil {
		t.Fatalf("purge tombstones: %v", err)
	}
	if purged != 1 {
		t.Fatalf("purged = %d, want 1", purged)
	}
	// 旧墓碑已过期，内容可以重新出现；新墓碑仍挡住同内容重传。
	if applied, _, err := s.ApplyRows(ctx, "cube-p", "dev-1", []domain.LocalRow{mkRow("old-1")}, limits); err != nil || applied != 1 {
		t.Fatalf("old tombstone expired = (%d,%v), want (1,nil)", applied, err)
	}
	if applied, unchanged, err := s.ApplyRows(ctx, "cube-p", "dev-1", []domain.LocalRow{mkRow("new-1")}, limits); err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("new tombstone = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
}

// TestPurgedDeletedRowDoesNotComeBack 软删除行被物理清理后留下墓碑：同内容重传
// 视为已删除，不会把用户删掉的记忆复活；内容变化才允许作为新行重新出现。
func TestPurgedDeletedRowDoesNotComeBack(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	limits := domain.LocalQuota{LimitBytes: 1 << 20, LimitRows: 10}
	row := func(hash string) domain.LocalRow {
		return domain.LocalRow{
			Layer:           domain.LocalLayerPolicy,
			SourceID:        "gone-1",
			ContentHash:     "sha256:" + strings.Repeat(hash, 64),
			Title:           "gone",
			Summary:         "gone",
			Payload:         map[string]any{"id": "gone-1"},
			PayloadGzip:     []byte(`{"id":"gone-1"}`),
			PayloadBytes:    15,
			SourceUpdatedAt: now,
		}
	}
	if _, _, err := s.ApplyRows(ctx, "cube-t", "dev-1", []domain.LocalRow{row("a")}, limits); err != nil {
		t.Fatalf("apply: %v", err)
	}
	memories, _, err := s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-t", PageSize: 10})
	if err != nil || len(memories) != 1 {
		t.Fatalf("list: %v len=%d", err, len(memories))
	}
	if err := s.DeleteMemory(ctx, "cube-t", memories[0].ID, now); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := s.PurgeDeletedBefore(ctx, now.Add(time.Hour)); err != nil {
		t.Fatalf("purge: %v", err)
	}

	// 同内容（甚至换一个设备重新绑定）不应复活。
	applied, unchanged, err := s.ApplyRows(ctx, "cube-t", "dev-2", []domain.LocalRow{row("a")}, limits)
	if err != nil || applied != 0 || unchanged != 1 {
		t.Fatalf("re-apply same content = (%d,%d,%v), want (0,1,nil)", applied, unchanged, err)
	}
	if memories, _, err = s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-t", PageSize: 10}); err != nil || len(memories) != 0 {
		t.Fatalf("revived: %v len=%d", err, len(memories))
	}

	// 内容变化才允许重新出现，并清掉墓碑。
	applied, unchanged, err = s.ApplyRows(ctx, "cube-t", "dev-2", []domain.LocalRow{row("b")}, limits)
	if err != nil || applied != 1 || unchanged != 0 {
		t.Fatalf("re-apply changed content = (%d,%d,%v), want (1,0,nil)", applied, unchanged, err)
	}
	if memories, _, err = s.ListMemories(ctx, domain.LocalMemoryFilter{TenantID: "cube-t", PageSize: 10}); err != nil || len(memories) != 1 {
		t.Fatalf("after change: %v len=%d", err, len(memories))
	}
}

// openRawDB 打开指向同一文件的裸连接，用于直接改测试准备数据（如回拨 synced_at）。
func openRawDB(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

// testTimeText 与存储层 encodeTime 使用同样的固定宽度毫秒 UTC 文本。
func testTimeText(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }
