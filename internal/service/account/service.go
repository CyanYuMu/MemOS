// Package account 管理控制台账号、项目空间成员关系与服务端会话。
package account

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/alexedwards/argon2id"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	platformthrottle "github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/throttle"
)

// Store 是账号服务需要的存储端口，由 adapter 结构化实现。按资源分组只为读起来清楚、
// 写测试替身时能按组挑选；Service 仍持有完整的 Store，因为登录、成员变更、删除账号
// 这些用例本来就跨资源。
type Store interface {
	TenantStore
	AccountStore
	MemberStore
	SessionStore
	InviteStore
}

// TenantStore 是账号服务读取项目、保证个人空间存在所需的部分。
type TenantStore interface {
	GetTenant(ctx context.Context, id string) (domain.Tenant, error)
	ListTenants(ctx context.Context) ([]domain.Tenant, error)
	PersonalTenantOf(ctx context.Context, accountID string) (domain.Tenant, error)
	EnsurePersonalTenant(ctx context.Context, t domain.Tenant, m domain.Membership) (domain.Tenant, error)
}

// AccountStore 是账号本身的读写。
type AccountStore interface {
	CreateAccount(ctx context.Context, a domain.Account) error
	GetAccount(ctx context.Context, id string) (domain.Account, error)
	AccountByUsername(ctx context.Context, username string) (domain.Account, error)
	ListAccounts(ctx context.Context) ([]domain.Account, error)
	ListAccountsPage(ctx context.Context, query string, limit, offset int) ([]domain.Account, int, error)
	SearchAccounts(ctx context.Context, query string, limit int) ([]domain.Account, error)
	UpdateAccount(ctx context.Context, a domain.Account) error
	// UpdateAccountAndRevokeSessions 在一个事务里更新账号并吊销其全部会话。
	UpdateAccountAndRevokeSessions(ctx context.Context, a domain.Account, at time.Time) error
	SetAccountProfile(ctx context.Context, id, displayName, email string) error
	SetAccountTenant(ctx context.Context, id, tenantID string) error
	TouchLogin(ctx context.Context, id string, at time.Time) error
	CountEnabledPlatformAdmins(ctx context.Context) (int, error)
	// DeleteAccount 删除账号及其控制面附属数据（个人项目、成员关系、会话）。
	DeleteAccount(ctx context.Context, id string) error
}

// MemberStore 是项目成员关系。
type MemberStore interface {
	PutMember(ctx context.Context, m domain.Membership) error
	RemoveMember(ctx context.Context, accountID, tenantID string) error
	ListMembers(ctx context.Context, tenantID string) ([]domain.Membership, error)
	MembershipsOf(ctx context.Context, accountID string) ([]domain.Membership, error)
}

// SessionStore 是服务端会话。
type SessionStore interface {
	CreateSession(ctx context.Context, s domain.Session) error
	SessionByHash(ctx context.Context, hash [32]byte) (domain.Session, error)
	GetSession(ctx context.Context, id string) (domain.Session, error)
	SetSessionTenant(ctx context.Context, id, tenantID string) error
	ExtendSession(ctx context.Context, id string, lastSeen, expiresAt time.Time) error
	RevokeSession(ctx context.Context, id string, at time.Time) error
	RevokeAccountSessions(ctx context.Context, accountID string, at time.Time) error
	RevokeAccountSessionsExcept(ctx context.Context, accountID, keepID string, at time.Time) (int64, error)
	ListSessions(ctx context.Context, accountID string) ([]domain.Session, error)
	PurgeSessionsBefore(ctx context.Context, before time.Time) (int64, error)
}

// InviteStore 是项目邀请链接。
type InviteStore interface {
	CreateInvite(ctx context.Context, inv domain.ProjectInvite) error
	GetInviteByHash(ctx context.Context, hash [32]byte) (domain.ProjectInvite, error)
	GetInvite(ctx context.Context, id string) (domain.ProjectInvite, error)
	ListInvites(ctx context.Context, tenantID string) ([]domain.ProjectInvite, error)
	// UpdateInviteRole 修改未使用邀请的角色。
	UpdateInviteRole(ctx context.Context, tenantID, id string, role domain.Role) error
	RevokeInvite(ctx context.Context, tenantID, id string, at time.Time) error
	RedeemInvite(ctx context.Context, in domain.RedeemInviteInput) error
	PurgeInvitesBefore(ctx context.Context, before time.Time) (int64, error)
}

// AdminAuthenticator 解析管理 Token；由 identity 服务实现，这里不 import 它。
type AdminAuthenticator interface {
	AuthenticateAdmin(ctx context.Context, raw string) (domain.Caller, error)
}

// Options 是账号服务的可调参数，零值取默认。
type Options struct {
	// SessionTTL 是会话空闲超时：每次请求最多每分钟把 expires_at 延到 now+SessionTTL。
	SessionTTL time.Duration
	// SessionMaxLifetime 是会话绝对上限：expires_at 不会超过 created_at+SessionMaxLifetime。
	SessionMaxLifetime time.Duration    // 默认 30 天，且不得小于 SessionTTL
	SeedUser           string           // 默认 "admin"
	SeedPassword       string           // 空表示不种子
	Argon2             *argon2id.Params // nil 用 OWASP 推荐值；测试可调小
	MaxFailures        int              // 默认 5
	FailWindow         time.Duration    // 默认 10m
	External           ExternalOptions  // 可信网关的外部身份；Enabled=false 时完全忽略
	// InviteDefaultTTL 是创建邀请未指定过期时间时的有效期，默认 7 天。
	InviteDefaultTTL time.Duration
	// InviteMaxTTL 是邀请允许的最长有效期，默认 30 天。
	InviteMaxTTL time.Duration
}

// ExternalOptions 决定网关身份如何映射到控制台账号。
type ExternalOptions struct {
	Enabled      bool
	Required     bool     // true：只认外部身份或管理 Token，本地会话单独使用时拒绝
	AutoCreate   bool     // true：未知 username 自动建无口令账号
	AllowedUsers []string // 空表示不限制；大小写不敏感
}

const (
	defaultSessionTTL         = 12 * time.Hour
	defaultSessionMaxLifetime = 30 * 24 * time.Hour
	defaultSeedUser           = "admin"
	defaultMaxFailures        = 5
	defaultFailWindow         = 10 * time.Minute
	touchInterval             = time.Minute
	purgeInterval             = time.Hour
	purgeGrace                = 24 * time.Hour
	sessionTokenBytes         = 32
	maxTouchTracked           = 10000
)

func owaspParams() *argon2id.Params {
	return &argon2id.Params{Memory: 19 * 1024, Iterations: 2, Parallelism: 1, SaltLength: 16, KeyLength: 32}
}

// Service 提供登录、会话认证、账号与成员管理。
type Service struct {
	store     Store
	admin     AdminAuthenticator
	opts      Options
	log       *slog.Logger
	dummyHash string
	throttle  Throttler
	now       func() time.Time

	// adminMu 串行化会改变平台管理员数量的写操作，保证“至少保留一个”。
	adminMu sync.Mutex
	// memberMu 保护 memberLocks 本身；成员写只锁项目自己的那把锁，
	// 不同项目的成员变更不再互相阻塞。
	memberMu    sync.Mutex
	memberLocks map[string]*sync.Mutex

	mu        sync.Mutex
	touched   map[string]time.Time
	lastPurge time.Time

	// cache 缓存会话哈希 → Principal，减少每个控制台请求的鉴权查询。
	// 账号、成员、会话状态变更时整体清空；租户禁用依赖 TTL 生效（默认 10s）。
	cacheMu sync.Mutex
	cache   map[[32]byte]cachedPrincipal
}

type cachedPrincipal struct {
	principal domain.Principal
	expires   time.Time
	// sessionExpires 是缓存建立时会话的过期时间：命中缓存也必须守住它，
	// 否则会话自然过期后最多还能凭缓存再通过 principalCacheTTL。
	// 吊销等主动失效走 invalidatePrincipalCache，不依赖这里。
	sessionExpires time.Time
}

const principalCacheTTL = 10 * time.Second

// Throttler 是登录失败节流端口。默认用进程内存，装配时可通过 WithThrottler
// 换成 Redis；两种实现共享同一套固定窗口语义。
type Throttler = platformthrottle.Throttler

// Option 在默认构造之后调整服务。
type Option func(*Service)

// WithThrottler 替换节流实现；传 nil 时保持默认的内存实现。
func WithThrottler(t Throttler) Option {
	return func(s *Service) {
		if t != nil {
			s.throttle = t
		}
	}
}

// WithLogger 注入日志器。
func WithLogger(log *slog.Logger) Option {
	return func(s *Service) {
		if log != nil {
			s.log = log
		}
	}
}

// New 构造账号服务并预先计算哑哈希（用于抹平“账号不存在”的耗时）。
func New(store Store, admin AdminAuthenticator, opts Options, options ...Option) (*Service, error) {
	log := slog.Default()
	if opts.SessionTTL <= 0 {
		opts.SessionTTL = defaultSessionTTL
	}
	if opts.SessionMaxLifetime <= 0 {
		opts.SessionMaxLifetime = defaultSessionMaxLifetime
	}
	if opts.SessionMaxLifetime < opts.SessionTTL {
		return nil, errors.New("account: session max lifetime must not be shorter than TTL")
	}
	if opts.SeedUser == "" {
		opts.SeedUser = defaultSeedUser
	}
	if opts.MaxFailures <= 0 {
		opts.MaxFailures = defaultMaxFailures
	}
	if opts.FailWindow <= 0 {
		opts.FailWindow = defaultFailWindow
	}
	if opts.InviteDefaultTTL <= 0 {
		opts.InviteDefaultTTL = defaultInviteTTL
	}
	if opts.InviteMaxTTL <= 0 {
		opts.InviteMaxTTL = defaultInviteMaxTTL
	}
	if opts.InviteMaxTTL < opts.InviteDefaultTTL {
		return nil, errors.New("account: invite max TTL must not be shorter than default TTL")
	}
	if opts.Argon2 == nil {
		opts.Argon2 = owaspParams()
	}
	if len(opts.External.AllowedUsers) > 0 {
		allowed := make([]string, 0, len(opts.External.AllowedUsers))
		for _, raw := range opts.External.AllowedUsers {
			if name := strings.ToLower(strings.TrimSpace(raw)); name != "" {
				allowed = append(allowed, name)
			}
		}
		opts.External.AllowedUsers = allowed
	}
	dummy, err := argon2id.CreateHash("dummy-password-for-timing", opts.Argon2)
	if err != nil {
		return nil, fmt.Errorf("account: dummy hash: %w", err)
	}
	s := &Service{
		store:     store,
		admin:     admin,
		opts:      opts,
		log:       log,
		dummyHash: dummy,
		throttle:  platformthrottle.NewMemory(opts.MaxFailures, opts.FailWindow),
		now:       time.Now,
		touched:   make(map[string]time.Time),
		cache:     make(map[[32]byte]cachedPrincipal),
	}
	for _, apply := range options {
		if apply != nil {
			apply(s)
		}
	}
	return s, nil
}

// Bootstrap 在账号不存在时创建种子管理员，补建个人项目，并清理过期很久的会话。
func (s *Service) Bootstrap(ctx context.Context) error {
	if s.opts.SeedPassword != "" {
		if err := s.seedAdmin(ctx); err != nil {
			return err
		}
	}
	s.ensurePersonalTenants(ctx)
	s.purgeInvites(ctx)
	return s.purgeExpired(ctx)
}

// purgeInvites 删除过期超过 30 天的邀请；失败只记 WARN，不影响启动。
func (s *Service) purgeInvites(ctx context.Context) {
	cutoff := s.now().Add(-invitePurgeAfter)
	n, err := s.store.PurgeInvitesBefore(ctx, cutoff)
	if err != nil {
		s.log.WarnContext(ctx, "purge invites failed", slog.Any("err", err))
		return
	}
	if n > 0 {
		s.log.InfoContext(ctx, "expired invites purged", slog.Int64("count", n))
	}
}

func (s *Service) seedAdmin(ctx context.Context) error {
	name, err := normalizeUsername(s.opts.SeedUser)
	if err != nil {
		return fmt.Errorf("account: seed user: %w", err)
	}
	if err := validatePassword(s.opts.SeedPassword); err != nil {
		return fmt.Errorf("account: seed password: %w", err)
	}
	existing, err := s.store.AccountByUsername(ctx, name)
	switch {
	case err == nil:
		// “存在就跳过”会让种子配置静默失效：零信任可能先用同名用户名自动建了
		// 空口令账号，之后配种子口令既不改口令也不提权，最后没有可登录的管理员。
		if existing.PasswordHash != "" {
			return nil
		}
		hash, err := argon2id.CreateHash(s.opts.SeedPassword, s.opts.Argon2)
		if err != nil {
			return fmt.Errorf("account: hash seed password: %w", err)
		}
		existing.PasswordHash = hash
		existing.PlatformAdmin = true
		if err := s.store.UpdateAccount(ctx, existing); err != nil {
			return err
		}
		s.log.WarnContext(ctx, "seed admin upgraded passwordless account",
			slog.String("username", name))
		return nil
	case !errors.Is(err, domain.ErrNotFound):
		return err
	}
	_, err = s.CreateAccount(ctx, CreateAccountInput{
		Username: name, Password: s.opts.SeedPassword, DisplayName: name, PlatformAdmin: true,
	})
	if errors.Is(err, domain.ErrConflict) {
		return nil
	}
	if err == nil {
		s.log.InfoContext(ctx, "seed admin created", slog.String("username", name))
	}
	return err
}

func (s *Service) purgeExpired(ctx context.Context) error {
	now := s.now()
	n, err := s.store.PurgeSessionsBefore(ctx, now.Add(-purgeGrace))
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.lastPurge = now
	s.mu.Unlock()
	if n > 0 {
		s.log.InfoContext(ctx, "expired sessions purged", slog.Int64("count", n))
	}
	return nil
}

func newSessionToken() (string, error) {
	buf := make([]byte, sessionTokenBytes)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("account: session token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}
