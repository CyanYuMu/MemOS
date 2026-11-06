package identity

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type fakeStore struct {
	mu      sync.Mutex
	tenants map[string]domain.Tenant
	tokens  map[string]*domain.Token
}

func newFakeStore() *fakeStore {
	return &fakeStore{tenants: map[string]domain.Tenant{}, tokens: map[string]*domain.Token{}}
}

func (f *fakeStore) GetTenant(_ context.Context, id string) (domain.Tenant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	t, ok := f.tenants[id]
	if !ok {
		return domain.Tenant{}, domain.NotFound("fake", "tenant")
	}
	return t, nil
}

func (f *fakeStore) UpsertTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) UpsertToken(_ context.Context, t domain.Token) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	stored := t
	if existing, ok := f.tokens[t.ID]; ok {
		stored.RevokedAt = existing.RevokedAt
	}
	f.tokens[t.ID] = &stored
	return nil
}

func (f *fakeStore) EnsureTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if existing, ok := f.tenants[t.ID]; ok {
		t.Disabled = existing.Disabled
	}
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) CreateToken(_ context.Context, t domain.Token) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, existing := range f.tokens {
		if existing.Hash == t.Hash {
			return domain.Wrap(domain.ErrConflict, "fake", "duplicate", nil)
		}
	}
	stored := t
	f.tokens[t.ID] = &stored
	return nil
}

func (f *fakeStore) TokenByHash(_ context.Context, hash [32]byte) (domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, t := range f.tokens {
		if t.Hash == hash {
			return *t, nil
		}
	}
	return domain.Token{}, domain.NotFound("fake", "token")
}

func (f *fakeStore) GetToken(_ context.Context, id string) (domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	t, ok := f.tokens[id]
	if !ok {
		return domain.Token{}, domain.NotFound("fake", "token")
	}
	return *t, nil
}

func (f *fakeStore) ListTokens(_ context.Context, tenantID string) ([]domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []domain.Token
	for _, t := range f.tokens {
		if tenantID == "" || t.TenantID == tenantID {
			out = append(out, *t)
		}
	}
	return out, nil
}

func (f *fakeStore) RevokeToken(_ context.Context, id string, at time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	t, ok := f.tokens[id]
	if !ok {
		return domain.NotFound("fake", "token")
	}
	t.RevokedAt = &at
	return nil
}

func (f *fakeStore) TouchToken(_ context.Context, id string, at time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	t, ok := f.tokens[id]
	if !ok {
		return domain.NotFound("fake", "token")
	}
	t.LastUsedAt = &at
	return nil
}

func (f *fakeStore) UpdateTokenLabel(_ context.Context, id, label string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	t, ok := f.tokens[id]
	if !ok {
		return domain.NotFound("fake", "token")
	}
	t.Label = label
	f.tokens[id] = t
	return nil
}

func TestBootstrapIsIdempotent(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-a", UserID: "alice", CubeID: "cube-a"}}, "admin-token-123456", nil)
	if err := svc.Bootstrap(context.Background()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if err := svc.Bootstrap(context.Background()); err != nil {
		t.Fatalf("Bootstrap 2: %v", err)
	}
	if len(fs.tenants) != 1 || len(fs.tokens) != 2 {
		t.Fatalf("tenants=%d tokens=%d", len(fs.tenants), len(fs.tokens))
	}
}

// 静态 Token 的展示名是给业务方看的「部署配置」，同一租户配重了要留日志。
func TestBootstrapLabelsStaticTokensAndWarnsOnDuplicateTenant(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	var logs bytes.Buffer
	svc := New(fs, []StaticTenant{
		{Token: "tok-a", UserID: "alice", CubeID: "cube-a"},
		{Token: "tok-b", UserID: "alice", CubeID: "cube-a"},
	}, "admin-token-123456", slog.New(slog.NewTextHandler(&logs, nil)))
	if err := svc.Bootstrap(context.Background()); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	if len(fs.tokens) != 3 {
		t.Fatalf("tokens=%d，两条静态 Token 与管理 Token 都应该写入", len(fs.tokens))
	}
	for id, tok := range fs.tokens {
		if tok.Label != staticKeyLabel {
			t.Fatalf("token %s label = %q，期望 %q", id, tok.Label, staticKeyLabel)
		}
	}
	if !strings.Contains(logs.String(), "多条 Token") {
		t.Fatalf("同一租户配多条 Token 应打 warn，实际日志: %s", logs.String())
	}
}

func TestAuthenticate(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-a", UserID: "alice", CubeID: "cube-a"}}, "admin-token-123456", nil)
	ctx := context.Background()
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	caller, err := svc.Authenticate(ctx, "tok-a")
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if caller.TenantID != "cube-a" || caller.UserID != "alice" || caller.TokenID == "" {
		t.Fatalf("caller = %+v", caller)
	}

	if _, err := svc.Authenticate(ctx, "wrong"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("wrong token err = %v", err)
	}
	if _, err := svc.Authenticate(ctx, "admin-token-123456"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("admin token used as tenant err = %v", err)
	}
	if _, err := svc.AuthenticateAdmin(ctx, "tok-a"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("tenant token used as admin err = %v", err)
	}
	admin, err := svc.AuthenticateAdmin(ctx, "admin-token-123456")
	if err != nil || !admin.Admin {
		t.Fatalf("admin caller = %+v, %v", admin, err)
	}

	// 禁用租户后鉴权失败。
	tenant := fs.tenants["cube-a"]
	tenant.Disabled = true
	fs.tenants["cube-a"] = tenant
	if _, err := svc.Authenticate(ctx, "tok-a"); !errors.Is(err, domain.ErrForbidden) {
		t.Fatalf("disabled tenant err = %v", err)
	}
}

func TestAuthenticateUsesCurrentTenantUserID(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-a", UserID: "alice", CubeID: "cube-a"}}, "", nil)
	ctx := context.Background()
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	// 管理端改绑上游 user_id 后，已签发的 Token 不能继续用旧身份写入。
	tenant := fs.tenants["cube-a"]
	tenant.UserID = "bob"
	fs.tenants["cube-a"] = tenant

	caller, err := svc.Authenticate(ctx, "tok-a")
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if caller.UserID != "bob" {
		t.Fatalf("caller.UserID = %q, want bob", caller.UserID)
	}
}

func TestBootstrapKeepsRevokedStaticToken(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-a", UserID: "alice", CubeID: "cube-a"}}, "", nil)
	ctx := context.Background()
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	tokens, err := svc.ListTokens(ctx, "cube-a")
	if err != nil || len(tokens) != 1 {
		t.Fatalf("ListTokens = %v, %v", tokens, err)
	}
	if err := svc.RevokeToken(ctx, tokens[0].ID); err != nil {
		t.Fatalf("RevokeToken: %v", err)
	}
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap 2: %v", err)
	}
	if _, err := svc.Authenticate(ctx, "tok-a"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("吊销的静态 Token 不应因重启复活: %v", err)
	}
}

func TestBootstrapKeepsDisabledTenant(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-a", UserID: "alice", CubeID: "cube-a"}}, "", nil)
	ctx := context.Background()
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}
	tenant := fs.tenants["cube-a"]
	tenant.Disabled = true
	if err := fs.UpsertTenant(ctx, tenant); err != nil {
		t.Fatalf("disable: %v", err)
	}
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap 2: %v", err)
	}
	if !fs.tenants["cube-a"].Disabled {
		t.Fatal("Bootstrap 不应重置 disabled")
	}
	if _, err := svc.Authenticate(ctx, "tok-a"); !errors.Is(err, domain.ErrForbidden) {
		t.Fatalf("禁用租户不应因重启恢复: %v", err)
	}
}

func TestExpiredTokenRejected(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-static", UserID: "alice", CubeID: "cube-a"}}, "", nil)
	base := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	svc.now = func() time.Time { return base }
	ctx := context.Background()
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	expiry := base.Add(5 * time.Second)
	raw, tok, err := svc.IssueTokenWithExpiry(ctx, TokenIssueInput{TenantID: "cube-a", Label: "short-lived", CreatedBy: "acct-1", ExpiresAt: &expiry})
	if err != nil {
		t.Fatalf("IssueTokenWithExpiry: %v", err)
	}
	if tok.ExpiresAt == nil || !tok.ExpiresAt.Equal(expiry) {
		t.Fatalf("签发结果应带过期时间: %+v", tok.ExpiresAt)
	}
	if _, err := svc.Authenticate(ctx, raw); err != nil {
		t.Fatalf("未过期 Token 应可用: %v", err)
	}

	// 缓存 TTL 是 10s；把时钟推到过期点但仍在缓存有效期内，必须再检查一次过期。
	svc.now = func() time.Time { return base.Add(5 * time.Second) }
	if _, err := svc.Authenticate(ctx, raw); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("缓存命中跨过过期点后应拒绝: %v", err)
	}

	// 静态 Token 没有 ExpiresAt，永远不过期。
	if _, err := svc.Authenticate(ctx, "tok-static"); err != nil {
		t.Fatalf("静态 Token 不应过期: %v", err)
	}

	// 走查库路径（无缓存）的过期判断。
	raw2, _, err := svc.IssueTokenWithExpiry(ctx, TokenIssueInput{TenantID: "cube-a", Label: "expired-now", ExpiresAt: &expiry})
	if err != nil {
		t.Fatalf("IssueTokenWithExpiry 2: %v", err)
	}
	if _, err := svc.Authenticate(ctx, raw2); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("过期 Token 首次查库就应拒绝: %v", err)
	}
}

func TestIssueAndRevoke(t *testing.T) {
	t.Parallel()

	fs := newFakeStore()
	svc := New(fs, []StaticTenant{{Token: "tok-a", UserID: "alice", CubeID: "cube-a"}}, "", nil)
	ctx := context.Background()
	if err := svc.Bootstrap(ctx); err != nil {
		t.Fatalf("Bootstrap: %v", err)
	}

	raw, tok, err := svc.IssueToken(ctx, "cube-a", "hermes", "acct-1")
	if err != nil {
		t.Fatalf("IssueToken: %v", err)
	}
	if tok.Hash != domain.HashToken(raw) {
		t.Fatal("stored hash does not match issued token")
	}
	if tok.CreatedByAccountID != "acct-1" {
		t.Fatalf("created_by = %q", tok.CreatedByAccountID)
	}
	if _, err := svc.Authenticate(ctx, raw); err != nil {
		t.Fatalf("issued token rejected: %v", err)
	}
	if err := svc.RevokeToken(ctx, tok.ID); err != nil {
		t.Fatalf("RevokeToken: %v", err)
	}
	if _, err := svc.Authenticate(ctx, raw); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("revoked token err = %v", err)
	}
	if _, _, err := svc.IssueToken(ctx, "missing", "", ""); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("issue for missing tenant err = %v", err)
	}
}
