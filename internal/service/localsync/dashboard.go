package localsync

import (
	"context"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// MemoryQuery 是控制台本地记忆列表的查询条件。
// StartTime/EndTime 按 source_updated_at 过滤，零值表示不设边界。
type MemoryQuery struct {
	Layer     string
	DeviceID  string
	Query     string
	StartTime time.Time
	EndTime   time.Time
	Page      int
	PageSize  int
}

// MemoryDetail 是详情响应：镜像元数据 + 解压后的 payload。
type MemoryDetail struct {
	Memory  domain.LocalMemory
	Payload map[string]any
}

// ListDevices 列出项目下的全部设备（含已吊销）。
func (s *Service) ListDevices(ctx context.Context, tenantID string) ([]domain.LocalDevice, error) {
	return s.store.ListDevices(ctx, tenantID)
}

// RevokeDevice 吊销设备令牌；只影响该设备，其他设备与项目数据不受影响。
func (s *Service) RevokeDevice(ctx context.Context, p domain.Principal, tenantID, deviceID string) error {
	if err := p.RequireCap(tenantID, domain.CapProjectWrite); err != nil {
		return err
	}
	deviceID = trimmed(deviceID)
	if deviceID == "" {
		return domain.Invalid("local_sync", "缺少设备 ID")
	}
	if err := s.store.RevokeDevice(ctx, tenantID, deviceID, s.now()); err != nil {
		return err
	}
	s.record(ctx, auditRecord("local_sync.device.revoke", tenantID, p.Username, map[string]any{
		"device_id": deviceID,
	}, nil))
	return nil
}

// Quota 返回项目配额与已用量，并补齐配置里的上限。
func (s *Service) Quota(ctx context.Context, tenantID string) (domain.LocalQuota, error) {
	return s.quotaView(ctx, tenantID)
}

// quotaView 汇总“已落库 + 未提交批次”的占用。准入校验（建批次、写分块）用的
// 就是这两部分之和；对外展示若只报已落库部分，用户会看到还有空间却收到 413。
func (s *Service) quotaView(ctx context.Context, tenantID string) (domain.LocalQuota, error) {
	quota, err := s.store.Quota(ctx, tenantID)
	if err != nil {
		return domain.LocalQuota{}, err
	}
	pendingBytes, pendingRows, err := s.store.PendingUsage(ctx, tenantID)
	if err != nil {
		return domain.LocalQuota{}, err
	}
	quota.PendingBytes = pendingBytes
	quota.PendingRows = pendingRows
	quota.LimitBytes = s.opts.QuotaBytes
	quota.LimitRows = s.opts.QuotaRows
	return quota, nil
}

// ListMemories 返回一页镜像摘要；列表不读取 payload。
func (s *Service) ListMemories(ctx context.Context, tenantID string, q MemoryQuery) ([]domain.LocalMemory, int, error) {
	layer := trimmed(q.Layer)
	if layer != "" {
		if _, ok := domain.ParseLocalLayer(layer); !ok {
			return nil, 0, domain.Invalid("local_sync", "未知 layer")
		}
	}
	if !q.StartTime.IsZero() && !q.EndTime.IsZero() && q.StartTime.After(q.EndTime) {
		return nil, 0, domain.Invalid("local_sync", "startTime 不能晚于 endTime")
	}
	return s.store.ListMemories(ctx, domain.LocalMemoryFilter{
		TenantID:  tenantID,
		Layer:     layer,
		DeviceID:  trimmed(q.DeviceID),
		Query:     trimmed(q.Query),
		StartTime: q.StartTime,
		EndTime:   q.EndTime,
		Page:      q.Page,
		PageSize:  q.PageSize,
	})
}

// MemoryDetail 读取单条镜像并解压 payload。
func (s *Service) MemoryDetail(ctx context.Context, tenantID, id string) (MemoryDetail, error) {
	memory, err := s.store.GetMemory(ctx, tenantID, trimmed(id))
	if err != nil {
		return MemoryDetail{}, err
	}
	payload, err := decompressPayload(memory.Payload)
	if err != nil {
		return MemoryDetail{}, err
	}
	return MemoryDetail{Memory: memory, Payload: payload}, nil
}

// DeleteMemory 软删除云端副本；本地数据不受影响，也不产生墓碑。
func (s *Service) DeleteMemory(ctx context.Context, p domain.Principal, tenantID, id string) error {
	if err := p.RequireCap(tenantID, domain.CapProjectWrite); err != nil {
		return err
	}
	id = trimmed(id)
	if id == "" {
		return domain.Invalid("local_sync", "缺少记忆 ID")
	}
	if err := s.store.DeleteMemory(ctx, tenantID, id, s.now()); err != nil {
		return err
	}
	s.record(ctx, auditRecord("local_sync.memory.delete", tenantID, p.Username, map[string]any{
		"memory_id": id,
	}, nil))
	return nil
}

// decompressPayload 由 compress.go 实现：解压镜像 payload，
// 上限与单行 payload 上限一致，防止历史脏数据把详情页拖死。
