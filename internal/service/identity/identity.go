package identity

import (
	"context"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Store 是 identity 需要的存储端口，由 adapter 结构化实现。
type Store interface {
	GetTenant(ctx context.Context, id string) (domain.Tenant, error)
	UpsertTenant(ctx context.Context, t domain.Tenant) error
	EnsureTenant(ctx context.Context, t domain.Tenant) error
	CreateToken(ctx context.Context, t domain.Token) error
	UpsertToken(ctx context.Context, t domain.Token) error
	TokenByHash(ctx context.Context, hash [32]byte) (domain.Token, error)
	GetToken(ctx context.Context, id string) (domain.Token, error)
	ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
	RevokeToken(ctx context.Context, id string, at time.Time) error
	// UpdateTokenLabel 修改 Token 的展示名。
	UpdateTokenLabel(ctx context.Context, id, label string) error
	TouchToken(ctx context.Context, id string, at time.Time) error
}

// StaticTenant 是启动配置里的静态租户与 Token。
type StaticTenant struct {
	Token  string
	UserID string
	CubeID string
}

// Service 负责 Token 到租户的解析与 Token 生命周期。
type Service struct {
	store     Store
	static    []StaticTenant
	adminHash *[32]byte
	log       *slog.Logger

	mu      sync.Mutex
	touched map[string]time.Time

	// authCache 缓存 Token 元数据，减少每个 MCP/REST 请求的 hash 查询。
	// 租户的 disabled 状态不缓存，每次仍然查库，保证停用立即生效。
	// 吊销与签发 Token 时整体清空。
	cacheMu sync.Mutex
	cache   map[[32]byte]cachedToken

	now func() time.Time
}

type cachedToken struct {
	token   domain.Token
	expires time.Time
}

const (
	touchInterval = time.Minute
	authCacheTTL  = 10 * time.Second
)

// staticKeyLabel 是启动配置（MEMOS_TENANTS 等）写入的密钥在控制台上的名字。
//
// 以前叫 "bootstrap"，那是代码里对“服务启动时引导出来的凭据”的叫法，属于内部
// 术语：业务方在密钥页看到两行同名 bootstrap，分不清是谁建的、能不能删。
// 现在直接用能读懂的名字；旧行由 UpsertToken 的 label 兜底逻辑一并升级。
const staticKeyLabel = "部署配置"

// New 构造身份服务。adminToken 为空表示不启用管理 Token。
func New(store Store, static []StaticTenant, adminToken string, log *slog.Logger) *Service {
	if log == nil {
		log = slog.Default()
	}
	s := &Service{
		store:   store,
		static:  static,
		log:     log,
		touched: make(map[string]time.Time),
		cache:   make(map[[32]byte]cachedToken),
		now:     time.Now,
	}
	if adminToken != "" {
		hash := domain.HashToken(adminToken)
		s.adminHash = &hash
	}
	return s
}

// Bootstrap 幂等写入静态租户、租户 Token 与管理 Token。
// 配置里已移除或已轮换的 bootstrap Token 会被吊销，避免旧凭据继续有效。
func (s *Service) Bootstrap(ctx context.Context) error {
	s.invalidateAuthCache()
	keep := map[string]struct{}{}
	// 同一租户配多条 Token 是合法的（轮换期新旧并存），但配置写重时两条都会
	// 变成同样名字的「部署配置」密钥，页面上看起来就是重复发了 Key 而没人发现。
	// 这里打一条 warn，把问题留在启动日志里。
	tenantTokens := map[string]string{}
	for _, st := range s.static {
		tenant := domain.Tenant{
			ID:        st.CubeID,
			UserID:    st.UserID,
			Name:      st.UserID,
			CreatedAt: s.now(),
		}
		if err := s.store.EnsureTenant(ctx, tenant); err != nil {
			return err
		}
		hash := domain.HashToken(st.Token)
		id := "static-" + hex.EncodeToString(hash[:6])
		keep[id] = struct{}{}
		if prev, ok := tenantTokens[st.CubeID]; ok {
			s.log.WarnContext(ctx, "静态租户配置了多条 Token，每一条都会生成一条密钥",
				slog.String("tenant", st.CubeID),
				slog.String("token_id", id),
				slog.String("other_token_id", prev))
		} else {
			tenantTokens[st.CubeID] = id
		}
		tok := domain.Token{
			ID:        id,
			Kind:      domain.TokenKindTenant,
			TenantID:  st.CubeID,
			UserID:    st.UserID,
			Hash:      hash,
			Prefix:    domain.TokenPrefix(st.Token),
			Raw:       st.Token,
			Label:     staticKeyLabel,
			CreatedAt: s.now(),
		}
		if err := s.store.UpsertToken(ctx, tok); err != nil {
			return err
		}
	}
	if s.adminHash != nil {
		id := "static-admin-" + hex.EncodeToString(s.adminHash[:6])
		keep[id] = struct{}{}
		tok := domain.Token{
			ID:        id,
			Kind:      domain.TokenKindAdmin,
			UserID:    "admin",
			Hash:      *s.adminHash,
			Prefix:    "admin",
			Label:     staticKeyLabel,
			CreatedAt: s.now(),
		}
		if err := s.store.UpsertToken(ctx, tok); err != nil {
			return err
		}
	}
	return s.revokeStaleBootstrap(ctx, keep)
}

// revokeStaleBootstrap 吊销配置中已不存在的 bootstrap Token。
func (s *Service) revokeStaleBootstrap(ctx context.Context, keep map[string]struct{}) error {
	tokens, err := s.store.ListTokens(ctx, "")
	if err != nil {
		return err
	}
	for i := range tokens {
		tok := tokens[i]
		// 只处理确定性 ID 的静态 Token；用户自建 Token 的 UUID 不以 static- 开头。
		// 不按 label 过滤：迁移导入的静态 Token 带展示名，配置里移除后同样必须吊销。
		if !strings.HasPrefix(tok.ID, "static-") || tok.RevokedAt != nil {
			continue
		}
		if _, ok := keep[tok.ID]; ok {
			continue
		}
		if err := s.store.RevokeToken(ctx, tok.ID, s.now()); err != nil {
			return err
		}
	}
	return nil
}

// Authenticate 解析租户 Token。Token 元数据命中短 TTL 缓存时不再按 hash 查库。
func (s *Service) Authenticate(ctx context.Context, raw string) (domain.Caller, error) {
	if raw == "" {
		return domain.Caller{}, unauthorized()
	}
	hash := domain.HashToken(raw)
	tok, ok := s.cachedToken(hash)
	if ok && tok.Expired(s.now()) {
		// 缓存命中也可能跨过过期点：缓存 TTL 是 10s，过期时间不以缓存为转移。
		return domain.Caller{}, unauthorized()
	}
	if !ok {
		var err error
		tok, err = s.lookup(ctx, raw, domain.TokenKindTenant)
		if err != nil {
			return domain.Caller{}, err
		}
		s.cacheToken(hash, tok)
	}
	tenant, err := s.store.GetTenant(ctx, tok.TenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.Caller{}, unauthorized()
		}
		return domain.Caller{}, err
	}
	if tenant.Disabled {
		return domain.Caller{}, domain.Wrap(domain.ErrForbidden, "authenticate", "该记忆空间已停用", nil)
	}
	s.touch(ctx, tok)
	// user_id 以租户当前值为准：管理端可以改绑 tenants.user_id，旧 Token 行里的
	// 值会随之过期。这里已经为 disabled 检查读过租户，派生 user_id 不增加查询；
	// 只有历史空值才回退到 Token 上保存的值。
	userID := tenant.UserID
	if userID == "" {
		userID = tok.UserID
	}
	return domain.Caller{TenantID: tok.TenantID, UserID: userID, TokenID: tok.ID}, nil
}

func (s *Service) cachedToken(hash [32]byte) (domain.Token, bool) {
	now := s.now()
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	entry, ok := s.cache[hash]
	if !ok || now.After(entry.expires) {
		delete(s.cache, hash)
		return domain.Token{}, false
	}
	return entry.token, true
}

func (s *Service) cacheToken(hash [32]byte, tok domain.Token) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	// 简单的容量保护：缓存随活跃 Token 数增长，超过阈值时整体清空。
	if len(s.cache) >= 4096 {
		clear(s.cache)
	}
	s.cache[hash] = cachedToken{token: tok, expires: s.now().Add(authCacheTTL)}
}

func (s *Service) invalidateAuthCache() {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	clear(s.cache)
}

// AuthenticateAdmin 只接受管理 Token。
func (s *Service) AuthenticateAdmin(ctx context.Context, raw string) (domain.Caller, error) {
	tok, err := s.lookup(ctx, raw, domain.TokenKindAdmin)
	if err != nil {
		return domain.Caller{}, err
	}
	s.touch(ctx, tok)
	return domain.Caller{TokenID: tok.ID, Admin: true}, nil
}

func (s *Service) lookup(ctx context.Context, raw string, want domain.TokenKind) (domain.Token, error) {
	if raw == "" {
		return domain.Token{}, unauthorized()
	}
	hash := domain.HashToken(raw)
	tok, err := s.store.TokenByHash(ctx, hash)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.Token{}, unauthorized()
		}
		return domain.Token{}, err
	}
	if subtle.ConstantTimeCompare(tok.Hash[:], hash[:]) != 1 {
		return domain.Token{}, unauthorized()
	}
	if tok.RevokedAt != nil || tok.Kind != want || tok.Expired(s.now()) {
		return domain.Token{}, unauthorized()
	}
	return tok, nil
}

// touch 最多每分钟更新一次 LastUsedAt；失败只影响统计，不影响鉴权。
func (s *Service) touch(ctx context.Context, tok domain.Token) {
	s.touchID(ctx, tok.ID)
}

func (s *Service) touchID(ctx context.Context, id string) {
	now := s.now()
	s.mu.Lock()
	last, ok := s.touched[id]
	if ok && now.Sub(last) < touchInterval {
		s.mu.Unlock()
		return
	}
	s.touched[id] = now
	s.mu.Unlock()

	if err := s.store.TouchToken(ctx, id, now); err != nil {
		s.log.DebugContext(ctx, "touch token failed", slog.String("error", err.Error()))
	}
}

// IssueToken 签发永不过期的租户 Token，明文只在这里返回一次。
func (s *Service) IssueToken(ctx context.Context, tenantID, label, createdBy string) (string, domain.Token, error) {
	return s.IssueTokenWithExpiry(ctx, TokenIssueInput{TenantID: tenantID, Label: label, CreatedBy: createdBy})
}

// TokenIssueInput 是签发 Token 的入参；ExpiresAt 为 nil 表示永不过期。
type TokenIssueInput struct {
	TenantID  string
	Label     string
	CreatedBy string
	ExpiresAt *time.Time
}

// IssueTokenWithExpiry 签发可过期的租户 Token；ExpiresAt 为 nil 表示永不过期。
// 过期时间只属于 Token 元数据，不向调用方暴露任何存储细节。
func (s *Service) IssueTokenWithExpiry(ctx context.Context, in TokenIssueInput) (string, domain.Token, error) {
	tenant, err := s.store.GetTenant(ctx, in.TenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return "", domain.Token{}, domain.NotFound("issue_token", "租户不存在")
		}
		return "", domain.Token{}, err
	}
	if tenant.Disabled {
		return "", domain.Token{}, domain.Wrap(domain.ErrForbidden, "issue_token", "该记忆空间已停用", nil)
	}
	raw, err := domain.NewRawToken()
	if err != nil {
		return "", domain.Token{}, err
	}
	tok := domain.Token{
		ID:       domain.NewID(),
		Kind:     domain.TokenKindTenant,
		TenantID: tenant.ID,
		UserID:   tenant.UserID,
		Hash:     domain.HashToken(raw),
		Prefix:   domain.TokenPrefix(raw),
		// 明文落库只为控制台「有写权限的成员再复制一次」（迁移 0024）。
		Raw:       raw,
		Label:     in.Label,
		CreatedAt: s.now(),
		ExpiresAt: in.ExpiresAt,

		CreatedByAccountID: in.CreatedBy,
	}
	if err := s.store.CreateToken(ctx, tok); err != nil {
		return "", domain.Token{}, err
	}
	s.invalidateAuthCache()
	return raw, tok, nil
}

// GetToken 按 ID 读取 Token 元数据，供 transport 先解析 Key 所属项目再判权。
func (s *Service) GetToken(ctx context.Context, id string) (domain.Token, error) {
	return s.store.GetToken(ctx, id)
}

// ListTokens 列出 Token；tenantID 为空时返回全部。
func (s *Service) ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error) {
	return s.store.ListTokens(ctx, tenantID)
}

// RevokeToken 吊销 Token。缓存整体清空，保证下一个请求立即失效。
func (s *Service) RevokeToken(ctx context.Context, id string) error {
	err := s.store.RevokeToken(ctx, id, s.now())
	s.invalidateAuthCache()
	return err
}

// RenameToken 修改 Token 的展示名。只影响 label，不改变哈希、前缀与过期时间；
// 缓存里缓存的 Token 元数据同步失效，避免后续请求读到旧名字。
func (s *Service) RenameToken(ctx context.Context, id, label string) error {
	err := s.store.UpdateTokenLabel(ctx, id, label)
	s.invalidateAuthCache()
	return err
}

func unauthorized() error {
	return domain.Wrap(domain.ErrUnauthorized, "authenticate",
		"无法确定调用方身份：请使用分配给自己的 Bearer token 访问本服务", nil)
}
