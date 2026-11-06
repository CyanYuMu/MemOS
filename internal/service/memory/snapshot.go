package memory

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	writeSnapshotVersion = 1
	maxSnapshotMemories  = 50
	maxSnapshotBytes     = 128 << 10
	snapshotTimeout      = 5 * time.Second
)

// 写入已经成功，读取快照的失败不能触发整次提取重试。
func (s *Service) captureWriteSnapshot(ctx context.Context, req domain.AddPayload, result *domain.AddResult) {
	if result.MemoryIDs == nil {
		return
	}
	readCtx, cancel := context.WithTimeout(ctx, snapshotTimeout)
	defer cancel()
	snapshot := &domain.MemoryWriteSnapshot{
		SchemaVersion: writeSnapshotVersion, TenantID: req.TenantID, UserID: req.UserID,
		SessionID: req.SessionID, CapturedAt: domain.UTCTime{Time: time.Now().UTC()},
		Memories: []domain.Memory{},
	}
	if body, err := json.Marshal(snapshot); err != nil || len(body) > maxSnapshotBytes {
		s.log.WarnContext(ctx, "memory snapshot envelope too large")
		return
	}
	ids := uniqueMemoryIDs(result.MemoryIDs)
	snapshot.Truncated = len(ids) > maxSnapshotMemories
	for _, id := range ids[:min(len(ids), maxSnapshotMemories)] {
		if readCtx.Err() != nil {
			break
		}
		mem, owned, err := s.up.OwnedMemory(readCtx, req.TenantID, req.UserID, id)
		if err != nil || !owned || mem.ID != id ||
			(mem.TenantID != "" && mem.TenantID != req.TenantID) ||
			(mem.UserID != "" && mem.UserID != req.UserID) {
			snapshot.Truncated = true
			continue
		}
		// 归属已经由OwnedMemory确认；只在投影补齐可信作用域，不改写底座。
		mem.TenantID, mem.UserID = req.TenantID, req.UserID
		mem.History, mem.Sources = nil, nil
		mem.Tags = append([]string(nil), mem.Tags...)
		snapshot.Memories = append(snapshot.Memories, mem)
		body, err := json.Marshal(snapshot)
		if err != nil || len(body) > maxSnapshotBytes {
			snapshot.Memories = snapshot.Memories[:len(snapshot.Memories)-1]
			snapshot.Truncated = true
		}
		if readCtx.Err() != nil {
			break
		}
	}
	if len(snapshot.Memories) != len(ids) {
		snapshot.Truncated = true
	}
	if snapshot.Truncated {
		s.log.WarnContext(ctx, "memory snapshot incomplete", slog.Int("confirmed_count", len(ids)), slog.Int("snapshot_count", len(snapshot.Memories)))
	}
	result.Snapshot = snapshot
}

func uniqueMemoryIDs(ids []string) []string {
	unique := []string{}
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id != "" && !seen[id] {
			seen[id] = true
			unique = append(unique, id)
		}
	}
	return unique
}

// 只信任同项目、同用户且属于该次确认集合的完整版本化快照。
func activitySnapshot(caller domain.Caller, output domain.MemoryWriteOutput) (*domain.MemoryWriteSnapshot, bool) {
	snapshot := output.Snapshot
	if snapshot == nil || snapshot.SchemaVersion != writeSnapshotVersion ||
		snapshot.TenantID != caller.TenantID || snapshot.UserID != caller.UserID || snapshot.CapturedAt.IsZero() {
		return nil, false
	}
	confirmed := make(map[string]bool, len(output.MemoryIDs))
	for _, id := range output.MemoryIDs {
		confirmed[id] = true
	}
	seen := make(map[string]bool, len(snapshot.Memories))
	for i := range snapshot.Memories {
		mem := &snapshot.Memories[i]
		if mem.ID == "" || !confirmed[mem.ID] || seen[mem.ID] || mem.TenantID != caller.TenantID || mem.UserID != caller.UserID {
			return nil, false
		}
		seen[mem.ID] = true
	}
	return snapshot, true
}
