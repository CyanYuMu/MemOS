package memory

import (
	"context"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func (s *Service) recordFailure(ctx context.Context, caller domain.Caller, kind string, err *error) {
	if *err != nil {
		s.recordEvent(ctx, caller, domain.MemoryEvent{Kind: kind, Status: "failed"})
	}
}

func (s *Service) recordRecall(ctx context.Context, caller domain.Caller, result domain.SearchResult, session string) {
	rows := append([]domain.Memory{}, result.Facts...)
	rows = append(rows, result.Preferences...)
	rows = append(rows, result.Tools...)
	rows = append(rows, result.Dreams...)
	ids := make([]string, 0, len(rows))
	owned := []domain.Memory{}
	truncated := false
	for i := range rows {
		mem := &rows[i]
		if (mem.TenantID != "" && mem.TenantID != caller.TenantID) || (mem.UserID != "" && mem.UserID != caller.UserID) {
			truncated = true
			continue
		}
		ids = append(ids, mem.ID)
		owned = append(owned, *mem)
	}
	s.recordEvent(ctx, caller, domain.MemoryEvent{Kind: "recall", Status: "succeeded", SessionID: session, IDs: ids, Memories: owned, Truncated: truncated})
}

func (s *Service) recordAddResult(ctx context.Context, req domain.AddPayload, result domain.AddResult, callID string) {
	event := domain.MemoryEvent{Kind: "add", Status: "succeeded", CallID: callID, SessionID: req.SessionID, IDs: result.MemoryIDs}
	if result.MemoryIDs == nil {
		event.Status = "unavailable"
		event.Truncated = true
	}
	if result.Snapshot != nil {
		event.Memories = result.Snapshot.Memories
		event.Truncated = result.Snapshot.Truncated
	} else if len(result.MemoryIDs) > 0 {
		event.Truncated = true
	}
	s.recordEvent(ctx, domain.Caller{TenantID: req.TenantID, UserID: req.UserID}, event)
}

func (s *Service) beforeDelete(ctx context.Context, caller domain.Caller, ids []string) ([]domain.Memory, bool) {
	if s.opts.Events == nil {
		return nil, false
	}
	result := domain.AddResult{MemoryIDs: ids}
	s.captureWriteSnapshot(ctx, domain.AddPayload{TenantID: caller.TenantID, UserID: caller.UserID}, &result)
	if result.Snapshot == nil {
		return nil, len(ids) > 0
	}
	return result.Snapshot.Memories, result.Snapshot.Truncated
}
