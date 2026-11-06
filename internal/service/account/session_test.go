package account

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// sessionStore 只实现会话认证与滑动续期用到的端口；其余方法由嵌入的
// Store 接口提供（不会被调用）。
type sessionStore struct {
	Store
	mu       sync.Mutex
	accounts map[string]domain.Account
	tenants  map[string]domain.Tenant
	sessions map[string]*domain.Session
	members  map[string][]domain.Membership
}

func newSessionStore() *sessionStore {
	return &sessionStore{
		accounts: map[string]domain.Account{},
		tenants:  map[string]domain.Tenant{},
		sessions: map[string]*domain.Session{},
		members:  map[string][]domain.Membership{},
	}
}

func (f *sessionStore) GetAccount(_ context.Context, id string) (domain.Account, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	acct, ok := f.accounts[id]
	if !ok {
		return domain.Account{}, domain.NotFound("get_account", "账号")
	}
	return acct, nil
}

func (f *sessionStore) ListTenants(context.Context) ([]domain.Tenant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]domain.Tenant, 0, len(f.tenants))
	for id := range f.tenants {
		out = append(out, f.tenants[id])
	}
	return out, nil
}

func (f *sessionStore) MembershipsOf(_ context.Context, accountID string) ([]domain.Membership, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]domain.Membership(nil), f.members[accountID]...), nil
}

func (f *sessionStore) SessionByHash(_ context.Context, hash [32]byte) (domain.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, sess := range f.sessions {
		if sess.Hash == hash {
			return *sess, nil
		}
	}
	return domain.Session{}, domain.NotFound("session_by_hash", "会话")
}

func (f *sessionStore) GetSession(_ context.Context, id string) (domain.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	sess, ok := f.sessions[id]
	if !ok {
		return domain.Session{}, domain.NotFound("get_session", "会话")
	}
	return *sess, nil
}

func (f *sessionStore) ExtendSession(_ context.Context, id string, lastSeen, expiresAt time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	sess, ok := f.sessions[id]
	if !ok {
		return domain.NotFound("extend_session", "会话")
	}
	if sess.RevokedAt != nil || !lastSeen.Before(sess.ExpiresAt) {
		return nil
	}
	sess.LastSeenAt = &lastSeen
	sess.ExpiresAt = expiresAt
	return nil
}

func (f *sessionStore) session() domain.Session {
	f.mu.Lock()
	defer f.mu.Unlock()
	return *f.sessions["sess-1"]
}

func (f *sessionStore) disableAccount(id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	acct := f.accounts[id]
	acct.Disabled = true
	f.accounts[id] = acct
}

func (f *sessionStore) setSessionExpiry(id string, expires time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sessions[id].ExpiresAt = expires
}

func newSessionService(t *testing.T) (*Service, *sessionStore, *time.Time) {
	t.Helper()
	fs := newSessionStore()
	now := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	svc, err := New(fs, nil, Options{
		SessionTTL:         12 * time.Hour,
		SessionMaxLifetime: 30 * 24 * time.Hour,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	svc.now = func() time.Time { return now }

	acct := domain.Account{ID: "acct-alice", Username: "alice", DisplayName: "alice"}
	fs.accounts[acct.ID] = acct
	fs.tenants["cube-a"] = domain.Tenant{ID: "cube-a", UserID: "alice", Name: "cube-a"}
	fs.members[acct.ID] = []domain.Membership{{AccountID: acct.ID, TenantID: "cube-a", Role: domain.RoleOwner}}
	raw := "session-raw-token"
	fs.sessions["sess-1"] = &domain.Session{
		ID: "sess-1", Hash: domain.HashToken(raw), AccountID: acct.ID,
		CreatedAt: now, ExpiresAt: now.Add(12 * time.Hour),
	}
	return svc, fs, &now
}

// TestSessionSlidingRenewal 覆盖空闲超时续期与绝对上限：
// 每次认证把 expires_at 延到 now+TTL，但不超过 created_at+MaxLifetime。
func TestSessionSlidingRenewal(t *testing.T) {
	t.Parallel()

	svc, fs, now := newSessionService(t)
	ctx := context.Background()
	const raw = "session-raw-token"
	created := *now

	// 首次认证在 created_at，过期时间保持 created+TTL。
	if _, err := svc.Authenticate(ctx, domain.Credentials{Session: raw}); err != nil {
		t.Fatalf("first authenticate: %v", err)
	}
	sess := fs.session()
	if sess.LastSeenAt == nil || !sess.ExpiresAt.Equal(created.Add(12*time.Hour)) {
		t.Fatalf("首次 touch = %+v", sess)
	}

	// 活跃用户的空闲超时随之滑动。
	*now = created.Add(6 * time.Hour)
	if _, err := svc.Authenticate(ctx, domain.Credentials{Session: raw}); err != nil {
		t.Fatalf("authenticate at 6h: %v", err)
	}
	sess = fs.session()
	if want := now.Add(12 * time.Hour); !sess.ExpiresAt.Equal(want) {
		t.Fatalf("6h 后 expires_at = %v, want %v", sess.ExpiresAt, want)
	}

	// 接近绝对上限时，过期时间被 created+MaxLifetime 截住。
	absolute := created.Add(30 * 24 * time.Hour)
	*now = absolute.Add(-2 * time.Hour)
	fs.setSessionExpiry("sess-1", absolute.Add(-time.Hour))
	if _, err := svc.Authenticate(ctx, domain.Credentials{Session: raw}); err != nil {
		t.Fatalf("authenticate near absolute cap: %v", err)
	}
	sess = fs.session()
	if !sess.ExpiresAt.Equal(absolute) {
		t.Fatalf("绝对上限处 expires_at = %v, want %v", sess.ExpiresAt, absolute)
	}

	// 上限前仍然正常，且不会越过上限继续延长。
	*now = absolute.Add(-30 * time.Minute)
	if _, err := svc.Authenticate(ctx, domain.Credentials{Session: raw}); err != nil {
		t.Fatalf("authenticate before absolute cap: %v", err)
	}
	if sess = fs.session(); !sess.ExpiresAt.Equal(absolute) {
		t.Fatalf("绝对上限后不再延长: %v", sess.ExpiresAt)
	}

	// 到达绝对上限即过期。
	*now = absolute
	if _, err := svc.Authenticate(ctx, domain.Credentials{Session: raw}); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("到达绝对上限应拒绝: %v", err)
	}
	if sess = fs.session(); !sess.ExpiresAt.Equal(absolute) {
		t.Fatalf("过期请求不应续期: %v", sess.ExpiresAt)
	}
}

func TestSessionAuthRejectsExpiredRevokedDisabled(t *testing.T) {
	t.Parallel()

	const raw = "session-raw-token"

	t.Run("expired", func(t *testing.T) {
		t.Parallel()
		svc, fs, now := newSessionService(t)
		fs.setSessionExpiry("sess-1", now.Add(-time.Second))
		if _, err := svc.Authenticate(context.Background(), domain.Credentials{Session: raw}); !errors.Is(err, domain.ErrUnauthorized) {
			t.Fatalf("过期会话应拒绝: %v", err)
		}
	})

	t.Run("revoked", func(t *testing.T) {
		t.Parallel()
		svc, fs, _ := newSessionService(t)
		revokedAt := time.Now()
		fs.sessions["sess-1"].RevokedAt = &revokedAt
		if _, err := svc.Authenticate(context.Background(), domain.Credentials{Session: raw}); !errors.Is(err, domain.ErrUnauthorized) {
			t.Fatalf("吊销会话应拒绝: %v", err)
		}
	})

	t.Run("disabled account", func(t *testing.T) {
		t.Parallel()
		svc, fs, _ := newSessionService(t)
		fs.disableAccount("acct-alice")
		if _, err := svc.Authenticate(context.Background(), domain.Credentials{Session: raw}); !errors.Is(err, domain.ErrUnauthorized) {
			t.Fatalf("禁用账号的会话应拒绝: %v", err)
		}
	})
}

func TestNewRejectsMaxLifetimeShorterThanTTL(t *testing.T) {
	t.Parallel()

	_, err := New(newSessionStore(), nil, Options{
		SessionTTL:         time.Hour,
		SessionMaxLifetime: 30 * time.Minute,
	})
	if err == nil {
		t.Fatal("SessionMaxLifetime < SessionTTL 应启动失败")
	}
}

func TestNewDefaultsSessionMaxLifetime(t *testing.T) {
	t.Parallel()

	svc, err := New(newSessionStore(), nil, Options{SessionTTL: time.Hour})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if svc.opts.SessionMaxLifetime != defaultSessionMaxLifetime {
		t.Fatalf("SessionMaxLifetime = %v", svc.opts.SessionMaxLifetime)
	}
}

// TestPrincipalCacheHonorsSessionExpiry 保证缓存命中不会越过会话过期时间：
// 缓存只活 10s，但若会话在缓存建立后 10s 内过期，命中时仍必须重新读库并拒绝。
func TestPrincipalCacheHonorsSessionExpiry(t *testing.T) {
	t.Parallel()

	svc, _, now := newSessionService(t)
	hash := domain.HashToken("session-raw-token")
	p := domain.Principal{Kind: domain.PrincipalAccount, AccountID: "acct-alice", SessionID: "sess-1"}
	svc.cachePrincipal(hash, p, now.Add(5*time.Second))

	if _, ok := svc.cachedPrincipal(hash); !ok {
		t.Fatal("会话未过期时应命中缓存")
	}

	*now = now.Add(6 * time.Second)
	if _, ok := svc.cachedPrincipal(hash); ok {
		t.Fatal("会话过期后不应再命中缓存")
	}
}

// TestTouchEveryScalesWithSessionTTL 保证续期节流小于空闲 TTL 的一半：
// 固定 1m 时，SessionTTL=1m 的下一次续期机会正好落在过期点，活跃用户会被登出。
func TestTouchEveryScalesWithSessionTTL(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name string
		ttl  time.Duration
		want time.Duration
	}{
		{"default 12h keeps 1m", 12 * time.Hour, time.Minute},
		{"2m halves to 1m boundary", 2 * time.Minute, time.Minute},
		{"1m halves to 30s", time.Minute, 30 * time.Second},
		{"3s halves to 1.5s", 3 * time.Second, 1500 * time.Millisecond},
		{"sub-second clamps to 1s", 1500 * time.Millisecond, time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			svc, err := New(newSessionStore(), nil, Options{
				SessionTTL:         tc.ttl,
				SessionMaxLifetime: 30 * 24 * time.Hour,
			})
			if err != nil {
				t.Fatalf("New: %v", err)
			}
			if got := svc.touchEvery(); got != tc.want {
				t.Fatalf("touchEvery(%v) = %v, want %v", tc.ttl, got, tc.want)
			}
		})
	}
}
