// Package localsync 实现本地插件记忆镜像的设备授权、分块上传、配额与转存。
package localsync

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 轮询授权码时的状态错误。transport 用 errors.Is 映射成 202 或 400。
var (
	ErrAuthorizationPending = errors.New("authorization pending")
	ErrAccessDenied         = errors.New("access denied")
	ErrExpiredToken         = errors.New("expired token")
)

// Store 是本地同步模块需要的存储端口，由 adapter/store 结构化实现。
type Store interface {
	CreateDeviceCode(ctx context.Context, c domain.LocalDeviceCode) error
	DeviceCodeByHash(ctx context.Context, hash [32]byte) (domain.LocalDeviceCode, error)
	DeviceCodeByUserCode(ctx context.Context, userCode string) (domain.LocalDeviceCode, error)
	ApproveDeviceCode(ctx context.Context, hash [32]byte, accountID, tenantID, label string, at time.Time) error
	DenyDeviceCode(ctx context.Context, hash [32]byte, at time.Time) error
	ConsumeDeviceCode(ctx context.Context, hash [32]byte, dev domain.LocalDevice, at time.Time) error
	DeviceByTokenHash(ctx context.Context, hash [32]byte) (domain.LocalDevice, error)
	TouchDevice(ctx context.Context, id string, at time.Time) error
	RevokeDevice(ctx context.Context, tenantID, deviceID string, at time.Time) error
	ListDevices(ctx context.Context, tenantID string) ([]domain.LocalDevice, error)

	GetMembership(ctx context.Context, accountID, tenantID string) (domain.Membership, error)
	MembershipsOf(ctx context.Context, accountID string) ([]domain.Membership, error)
	GetAccount(ctx context.Context, id string) (domain.Account, error)
	GetTenant(ctx context.Context, id string) (domain.Tenant, error)
	ListTenants(ctx context.Context) ([]domain.Tenant, error)

	CreateBatchWithinQuota(ctx context.Context, b domain.LocalSyncBatch, quotaBytes, quotaRows int64) error
	GetBatch(ctx context.Context, tenantID, deviceID, batchID string) (domain.LocalSyncBatch, error)
	PutChunk(ctx context.Context, tenantID, deviceID, batchID string, seq int, body []byte, maxBatchBytes, quotaBytes int64, at time.Time) (int64, error)
	ListChunkSeqs(ctx context.Context, tenantID, deviceID, batchID string) ([]int, error)
	GetChunk(ctx context.Context, tenantID, deviceID, batchID string, seq int) ([]byte, error)
	FinishBatch(ctx context.Context, tenantID, deviceID, batchID string, result domain.LocalCommitResult, at time.Time) error
	PurgeBatchesBefore(ctx context.Context, before time.Time) (int64, error)
	PurgeMirroredBefore(ctx context.Context, before time.Time) (int64, error)
	PurgeTombstonesBefore(ctx context.Context, before time.Time) (int64, error)
	PurgeDeletedBefore(ctx context.Context, before time.Time) (int64, error)

	ApplyRows(ctx context.Context, tenantID, deviceID string, rows []domain.LocalRow, limits domain.LocalQuota) (applied, unchanged int64, err error)
	LocalState(ctx context.Context, tenantID, deviceID string) (domain.LocalState, error)
	Quota(ctx context.Context, tenantID string) (domain.LocalQuota, error)
	PendingUsage(ctx context.Context, tenantID string) (bytes, rows int64, err error)

	ListMemories(ctx context.Context, f domain.LocalMemoryFilter) ([]domain.LocalMemory, int, error)
	SummarizeMemories(ctx context.Context, tenantID string) (domain.LocalMemorySummary, error)
	GetMemory(ctx context.Context, tenantID, id string) (domain.LocalMemory, error)
	DeleteMemory(ctx context.Context, tenantID, id string, at time.Time) error
	MarkPromoteQueued(ctx context.Context, tenantID, id, jobID string, at time.Time) error
	MarkPromoteResult(ctx context.Context, tenantID, id, status, jobID, message string, at time.Time) error
	EnqueueJob(ctx context.Context, j domain.Job) error
}

// Adder 由 service/memory 实现；转存作业同步调用上游 add_message。
type Adder interface {
	Add(ctx context.Context, caller domain.Caller, req domain.AddRequest) (domain.AddResult, error)
}

// Auditor 是审计端口，由 service/audit 实现；为 nil 时不写审计。
type Auditor interface {
	Record(ctx context.Context, rec domain.AuditRecord)
	RecordNow(ctx context.Context, rec domain.AuditRecord) error
}

// Options 是本模块的配置。零值字段由 New 填默认值。
type Options struct {
	DeviceCodeTTL time.Duration
	PollInterval  time.Duration
	TokenTTL      time.Duration
	BatchTTL      time.Duration
	MaxChunkBytes int64
	MaxBatchBytes int64
	// MaxBatchDecompressed 是一次 commit 全部块解压后的总量上限。
	MaxBatchDecompressed int64
	MaxRowBytes          int
	QuotaBytes           int64
	QuotaRows            int64
	RatePerMinute        int
	// DeletedRetention 是软删除行的物理清理期限；0 表示不清理。
	DeletedRetention time.Duration
	// MirrorRetention 是控制面镜像的保留期；0 表示永久保留。只影响副本。
	MirrorRetention time.Duration
	// TombstoneRetention 是删除墓碑的保留期；0 表示永不清理。
	TombstoneRetention time.Duration
	// VerificationURI 是设备授权页的绝对地址；为空时由 transport 按请求推导。
	VerificationURI string
}

const (
	defaultDeviceCodeTTL = 10 * time.Minute
	defaultPollInterval  = 5 * time.Second
	defaultTokenTTL      = 180 * 24 * time.Hour
	defaultBatchTTL      = 24 * time.Hour
	defaultRatePerMinute = 120
	touchInterval        = time.Minute
	// maxRejectedReasons 限制 commit 响应里的拒因条数；计数仍然完整。
	maxRejectedReasons = 100
	// maxDecompressedChunk 是单块解压后的硬上限，防止压缩炸弹。
	maxDecompressedChunk = 32 << 20
	// maxPayloadDetailBytes 是详情页解压 payload 的上限。
	maxPayloadDetailBytes = 1 << 20
)

// Service 实现本地同步的用例。
type Service struct {
	store Store
	adder Adder
	audit Auditor
	opts  Options
	log   *slog.Logger
	now   func() time.Time

	writes    *rateLimiter
	codeCalls *rateLimiter

	touchMu   sync.Mutex
	lastTouch map[string]time.Time
}

// New 构造服务。缺省值在这里补齐，调用方不需要重复配置。
func New(store Store, adder Adder, audit Auditor, opts Options, log *slog.Logger) *Service {
	if opts.DeviceCodeTTL <= 0 {
		opts.DeviceCodeTTL = defaultDeviceCodeTTL
	}
	if opts.PollInterval <= 0 {
		opts.PollInterval = defaultPollInterval
	}
	if opts.TokenTTL <= 0 {
		opts.TokenTTL = defaultTokenTTL
	}
	if opts.BatchTTL <= 0 {
		opts.BatchTTL = defaultBatchTTL
	}
	if opts.MaxChunkBytes <= 0 {
		opts.MaxChunkBytes = domain.LocalChunkBytesLimit
	}
	if opts.MaxBatchBytes <= 0 {
		opts.MaxBatchBytes = domain.LocalBatchBytesLimit
	}
	if opts.MaxBatchDecompressed <= 0 {
		opts.MaxBatchDecompressed = domain.LocalBatchDecompressedLimit
	}
	if opts.MaxRowBytes <= 0 {
		opts.MaxRowBytes = domain.LocalRowPayloadLimit
	}
	if opts.QuotaBytes <= 0 {
		opts.QuotaBytes = domain.LocalQuotaBytesLimit
	}
	if opts.QuotaRows <= 0 {
		opts.QuotaRows = domain.LocalRowCountLimit
	}
	if opts.RatePerMinute <= 0 {
		opts.RatePerMinute = defaultRatePerMinute
	}
	if log == nil {
		log = slog.Default()
	}
	now := time.Now
	return &Service{
		store:     store,
		adder:     adder,
		audit:     audit,
		opts:      opts,
		log:       log,
		now:       now,
		writes:    newRateLimiter(opts.RatePerMinute, now),
		codeCalls: newRateLimiter(opts.RatePerMinute, now),
		lastTouch: make(map[string]time.Time),
	}
}

// Options 返回生效后的配置（含默认值），供 transport 读取上限。
func (s *Service) Options() Options { return s.opts }

// DeviceIdentity 是设备令牌解析出的调用身份，只在本进程内使用。
type DeviceIdentity struct {
	DeviceID  string
	TenantID  string
	AccountID string
	Username  string
	Label     string
	AgentKind string
	Role      domain.Role
}

// AuthenticateDevice 解析设备令牌，并**每次请求**复核账号、成员关系与角色。
func (s *Service) AuthenticateDevice(ctx context.Context, raw string) (DeviceIdentity, error) {
	if !domain.IsLocalSyncToken(raw) {
		return DeviceIdentity{}, unauthorized()
	}
	dev, err := s.store.DeviceByTokenHash(ctx, domain.HashLocalSyncToken(raw))
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return DeviceIdentity{}, unauthorized()
		}
		return DeviceIdentity{}, err
	}
	if !dev.Active(s.now(), s.opts.TokenTTL) {
		return DeviceIdentity{}, unauthorized()
	}
	acct, err := s.store.GetAccount(ctx, dev.AccountID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return DeviceIdentity{}, unauthorized()
		}
		return DeviceIdentity{}, err
	}
	if acct.Disabled {
		return DeviceIdentity{}, unauthorized()
	}
	member, err := s.store.GetMembership(ctx, dev.AccountID, dev.TenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return DeviceIdentity{}, forbidden()
		}
		return DeviceIdentity{}, err
	}
	if !member.Role.Can(domain.CapLocalSync) {
		return DeviceIdentity{}, forbidden()
	}
	tenant, err := s.store.GetTenant(ctx, dev.TenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return DeviceIdentity{}, forbidden()
		}
		return DeviceIdentity{}, err
	}
	if tenant.Disabled {
		return DeviceIdentity{}, forbidden()
	}
	s.touchDevice(ctx, dev.ID)
	return DeviceIdentity{
		DeviceID:  dev.DeviceID,
		TenantID:  dev.TenantID,
		AccountID: dev.AccountID,
		Username:  acct.Username,
		Label:     dev.Label,
		AgentKind: dev.AgentKind,
		Role:      member.Role,
	}, nil
}

// touchDevice 对 last_seen_at 的写入做 1 分钟节流；失败只记 WARN，不影响请求。
func (s *Service) touchDevice(ctx context.Context, id string) {
	now := s.now()
	s.touchMu.Lock()
	last, ok := s.lastTouch[id]
	if ok && now.Sub(last) < touchInterval {
		s.touchMu.Unlock()
		return
	}
	s.lastTouch[id] = now
	if len(s.lastTouch) > 10_000 {
		for key, at := range s.lastTouch {
			if now.Sub(at) > touchInterval*2 {
				delete(s.lastTouch, key)
			}
		}
	}
	s.touchMu.Unlock()

	if err := s.store.TouchDevice(ctx, id, now); err != nil {
		s.log.WarnContext(ctx, "touch local device failed",
			slog.String("device_record", id), slog.String("error", err.Error()))
	}
}

func (s *Service) allowWrite(identity DeviceIdentity) error {
	if s.writes.allow(identity.DeviceID) {
		return nil
	}
	return domain.Wrap(domain.ErrRateLimited, "local_sync", "请求过于频繁，请稍后再试", nil)
}

func (s *Service) record(ctx context.Context, rec domain.AuditRecord) {
	if s.audit == nil {
		return
	}
	s.audit.Record(ctx, rec)
}

func (s *Service) recordNow(ctx context.Context, rec domain.AuditRecord) error {
	if s.audit == nil {
		return nil
	}
	return s.audit.RecordNow(ctx, rec)
}

func auditRecord(tool, tenantID, userID string, input map[string]any, err error) domain.AuditRecord {
	rec := domain.AuditRecord{
		ID:       domain.NewID(),
		CallID:   domain.NewID(),
		TS:       time.Now(),
		Tool:     tool,
		TenantID: tenantID,
		UserID:   userID,
		OK:       err == nil,
		Input:    input,
	}
	if err != nil {
		rec.Err = errorName(err)
		rec.ErrMessage = domain.SafeMessage(err)
	}
	return rec
}

func errorName(err error) string {
	if kind := domain.KindOf(err); kind != nil {
		return kind.Error()
	}
	return "internal"
}

func unauthorized() error {
	return domain.Wrap(domain.ErrUnauthorized, "local_sync", "设备令牌无效或已过期", nil)
}

func forbidden() error {
	return domain.Wrap(domain.ErrForbidden, "local_sync", "设备所属账号已无该项目的上传权限", nil)
}

// rateLimiter 是进程内滑动窗口限流。多副本部署时各自计数，与登录节流的内存实现同口径。
type rateLimiter struct {
	mu       sync.Mutex
	limit    int
	window   time.Duration
	now      func() time.Time
	counters map[string]windowCount
}

type windowCount struct {
	start time.Time
	count int
}

func newRateLimiter(limit int, now func() time.Time) *rateLimiter {
	return &rateLimiter{
		limit:    max(limit, 1),
		window:   time.Minute,
		now:      now,
		counters: make(map[string]windowCount),
	}
}

func (l *rateLimiter) allow(key string) bool {
	if key == "" {
		return true
	}
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	c, ok := l.counters[key]
	if !ok || now.Sub(c.start) >= l.window {
		c = windowCount{start: now}
	}
	c.count++
	l.counters[key] = c
	if len(l.counters) > 10_000 {
		for k, v := range l.counters {
			if now.Sub(v.start) >= l.window {
				delete(l.counters, k)
			}
		}
	}
	return c.count <= l.limit
}

// safeText 截断对外错误文案，避免把内部细节带出去。
func safeText(err error) string {
	if err == nil {
		return ""
	}
	return domain.TruncateRunes(domain.SafeMessage(err), 200)
}

func trimmed(value string) string { return strings.TrimSpace(value) }
