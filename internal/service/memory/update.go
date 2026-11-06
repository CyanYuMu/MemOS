package memory

import (
	"context"
	"crypto/sha256"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// MemoryUpdater 单独声明，避免只读适配器也被迫提供写入能力。
type MemoryUpdater interface {
	UpdateMemory(context.Context, domain.MemoryUpdate) error
}

// Update 只改当前空间的现存记录，读回之后才向调用方确认成功。
func (s *Service) Update(ctx context.Context, caller domain.Caller, in domain.MemoryUpdate) (result domain.MemoryUpdateResult, err error) {
	defer s.observe("update", &err)
	if err := requireTenant(caller); err != nil {
		return result, err
	}
	defer s.recordFailure(ctx, caller, "update", &err)
	in.ID, in.Content = strings.TrimSpace(in.ID), strings.TrimSpace(in.Content)
	if in.ID == "" || in.Content == "" {
		return result, domain.Invalid("update", "请选择记忆并填写新的内容")
	}
	in.TenantID, in.UserID = caller.TenantID, caller.UserID
	// 固定分片不会随记忆数量增长；本实例的同ID更新覆盖完整读写读。
	key := sha256.Sum256([]byte(in.TenantID + "\x00" + in.UserID + "\x00" + in.ID))
	lock := s.updateLocks[key[0]%64]
	select {
	case lock <- struct{}{}:
		defer func() { <-lock }()
	case <-ctx.Done():
		return result, ctx.Err()
	}
	if err := ctx.Err(); err != nil {
		return result, err
	}
	current, owned, err := s.up.OwnedMemory(ctx, caller.TenantID, caller.UserID, in.ID)
	if err != nil {
		return result, err
	}
	if !owned {
		return result, domain.Wrap(domain.ErrNotFound, "update", "未找到这条记忆", nil)
	}
	if current.Content == in.Content && (in.Title == nil || current.Key == *in.Title) {
		return domain.MemoryUpdateResult{Memory: current}, nil
	}
	updater, ok := s.up.(MemoryUpdater)
	if !ok {
		return result, domain.Wrap(domain.ErrDisabled, "update", "暂时无法修改记忆，请稍后重试", nil)
	}
	in.ResetContentTags = current.Content != in.Content
	if err := updater.UpdateMemory(ctx, in); err != nil {
		return result, err
	}
	updated, owned, err := s.up.OwnedMemory(ctx, caller.TenantID, caller.UserID, in.ID)
	if err != nil {
		return result, err
	}
	if !owned || updated.Content != in.Content || (in.Title != nil && updated.Key != *in.Title) {
		return result, domain.Wrap(domain.ErrConflict, "update", "尚未确认修改结果，请查看记忆详情", nil)
	}
	if current.Content != in.Content && updated.Version != current.Version+1 {
		return result, domain.Wrap(domain.ErrConflict, "update", "尚未确认修改结果，请查看记忆详情", nil)
	}
	if in.ResetContentTags && len(updated.Tags) != 0 {
		return result, domain.Wrap(domain.ErrConflict, "update", "尚未确认修改结果，请查看记忆详情", nil)
	}
	s.recordEvent(ctx, caller, domain.MemoryEvent{Kind: "update", Status: "succeeded", IDs: []string{in.ID}, Memories: []domain.Memory{updated}, Before: []domain.Memory{current}})
	return domain.MemoryUpdateResult{Memory: updated, Changed: true}, nil
}
