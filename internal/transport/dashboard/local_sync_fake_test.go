package dashboard

import (
	"context"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	localsync "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/localsync"
)

// fakeLocalSync 给 Dashboard 的形状测试提供固定数据；不涉及存储与判权，
// 判权由 transport/auth 的规则层负责。
type fakeLocalSync struct{}

var fakeLocalSyncAt = time.Date(2026, 10, 24, 8, 0, 0, 0, time.UTC)

func (fakeLocalSync) ListDevices(context.Context, string) ([]domain.LocalDevice, error) {
	seen := fakeLocalSyncAt
	return []domain.LocalDevice{{
		ID:         "ldev-1",
		AccountID:  "acct-alice",
		TenantID:   "cube-a",
		DeviceID:   "dev-1",
		Label:      "laptop",
		AgentKind:  "claude-code",
		Status:     domain.LocalDeviceActive,
		CreatedAt:  fakeLocalSyncAt,
		LastSeenAt: &seen,
	}}, nil
}

func (fakeLocalSync) RevokeDevice(context.Context, domain.Principal, string, string) error {
	return nil
}

func (fakeLocalSync) Quota(context.Context, string) (domain.LocalQuota, error) {
	return fakeLocalQuota(), nil
}

func (fakeLocalSync) ListMemories(context.Context, string, localsync.MemoryQuery) ([]domain.LocalMemory, int, error) {
	return []domain.LocalMemory{fakeLocalMemory()}, 1, nil
}

func (fakeLocalSync) Summary(context.Context, string) (domain.LocalMemorySummary, error) {
	return domain.LocalMemorySummary{
		TenantID:       "cube-a",
		Layers:         map[string]int64{"trace": 1},
		Total:          1,
		Promoted:       1,
		OldestSyncedAt: &fakeLocalSyncAt,
		NewestSyncedAt: &fakeLocalSyncAt,
		Quota:          fakeLocalQuota(),
	}, nil
}

func (fakeLocalSync) MemoryDetail(context.Context, string, string) (localsync.MemoryDetail, error) {
	return localsync.MemoryDetail{
		Memory:  fakeLocalMemory(),
		Payload: map[string]any{"summary": "s", "tags": []any{"t"}},
	}, nil
}

func (fakeLocalSync) DeleteMemory(context.Context, domain.Principal, string, string) error {
	return nil
}

func (fakeLocalSync) Promote(context.Context, domain.Principal, string, []string) (domain.LocalPromoteResult, error) {
	return domain.LocalPromoteResult{Queued: []string{"lmem-1"}}, nil
}

func fakeLocalQuota() domain.LocalQuota {
	return domain.LocalQuota{
		UsedBytes:    1024,
		LimitBytes:   domain.LocalQuotaBytesLimit,
		UsedRows:     2,
		LimitRows:    domain.LocalRowCountLimit,
		PendingBytes: 128,
		PendingRows:  1,
	}
}

func fakeLocalMemory() domain.LocalMemory {
	return domain.LocalMemory{
		ID:              "lmem-1",
		TenantID:        "cube-a",
		DeviceID:        "dev-1",
		DeviceLabel:     "laptop",
		Layer:           domain.LocalLayerTrace,
		SourceID:        "src-1",
		ContentHash:     "sha256:abc",
		Title:           "t",
		Summary:         "s",
		SourceUpdatedAt: fakeLocalSyncAt,
		PromoteStatus:   domain.LocalPromoteSucceeded,
		SyncedAt:        fakeLocalSyncAt,
	}
}
