package service

import (
	"context"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// AuditFilter 是审计查询条件；定义在 domain，adapter 与 service 共用同一类型。
type AuditFilter = domain.AuditFilter

// TenantRepo 是租户持久化端口。
type TenantRepo interface {
	GetTenant(ctx context.Context, id string) (domain.Tenant, error)
	UpsertTenant(ctx context.Context, t domain.Tenant) error
	ListTenants(ctx context.Context) ([]domain.Tenant, error)
}

// TokenRepo 是 Token 元数据持久化端口。明文 Token 永远不经过这里。
type TokenRepo interface {
	CreateToken(ctx context.Context, t domain.Token) error
	UpsertToken(ctx context.Context, t domain.Token) error
	TokenByHash(ctx context.Context, hash [32]byte) (domain.Token, error)
	GetToken(ctx context.Context, id string) (domain.Token, error)
	ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
	RevokeToken(ctx context.Context, id string, at time.Time) error
	TouchToken(ctx context.Context, id string, at time.Time) error
}

// AuditRepo 是审计持久化端口。
type AuditRepo interface {
	InsertAudit(ctx context.Context, r domain.AuditRecord) error
	InsertAudits(ctx context.Context, records []domain.AuditRecord) error
	ListAudit(ctx context.Context, f AuditFilter) ([]domain.AuditRecord, int, error)
	GetAudit(ctx context.Context, id string) (domain.AuditRecord, error)
	AuditHourCounts(ctx context.Context, f AuditFilter) ([]domain.AuditHourCount, error)
	PurgeAuditBefore(ctx context.Context, before time.Time) (int64, error)
}

// JobRepo 是异步任务持久化端口。
type JobRepo interface {
	EnqueueJob(ctx context.Context, j domain.Job) error
	ClaimJob(ctx context.Context, now time.Time) (domain.Job, bool, error)
	ResetStaleJobs(ctx context.Context, now, staleBefore time.Time) (int64, error)
	RescheduleJob(ctx context.Context, p domain.JobReschedule) error
	FinishJob(ctx context.Context, p domain.JobCompletion) error
}

// Repository 是控制面自有数据的聚合端口，由 app 注入具体实现。
type Repository interface {
	TenantRepo
	TokenRepo
	AuditRepo
	JobRepo
	Ping(ctx context.Context) error
	Close() error
}
