package localsync

import (
	"context"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Summary 返回项目本地镜像的治理视图。调用方需已完成项目读权限校验。
func (s *Service) Summary(ctx context.Context, tenantID string) (domain.LocalMemorySummary, error) {
	tenantID = trimmed(tenantID)
	if tenantID == "" {
		return domain.LocalMemorySummary{}, domain.Invalid("local_sync", "缺少 projectId")
	}
	summary, err := s.store.SummarizeMemories(ctx, tenantID)
	if err != nil {
		return domain.LocalMemorySummary{}, err
	}
	// 存储层只统计已落库用量；这里补上未提交批次与配置里的上限，保持与准入同口径。
	quota, err := s.quotaView(ctx, tenantID)
	if err != nil {
		return domain.LocalMemorySummary{}, err
	}
	summary.Quota = quota
	return summary, nil
}
