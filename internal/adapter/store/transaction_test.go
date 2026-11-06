package store_test

import (
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// openStoreWithPath 返回存储实例与库文件路径，后者用于注入失败触发器。
func openStoreWithPath(t *testing.T) (*store.Store, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	s, err := store.Open(t.Context(), path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s, path
}

// execRaw 用独立连接执行一条 SQL（建/删触发器）。SQLite 的 WAL 下 schema
// 变更对所有连接可见，store 自己的写事务会立即看到触发器。
func execRaw(t *testing.T, path, stmt string) {
	t.Helper()
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	defer func() { _ = db.Close() }()
	if _, err := db.ExecContext(t.Context(), stmt); err != nil {
		t.Fatalf("exec %q: %v", stmt, err)
	}
}

func TestCreateTenantWithOwnerCommitsBothRows(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)
	tenant := domain.Tenant{ID: "team-a", UserID: "team-a", Name: "team-a", Kind: domain.TenantKindTeam}

	if err := s.CreateTenantWithOwner(ctx, tenant, acct.ID, domain.RoleOwner); err != nil {
		t.Fatalf("CreateTenantWithOwner: %v", err)
	}
	if _, err := s.GetTenant(ctx, tenant.ID); err != nil {
		t.Fatalf("租户行应已写入: %v", err)
	}
	members, err := s.ListMembers(ctx, tenant.ID)
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	if len(members) != 1 || members[0].AccountID != acct.ID || members[0].Role != domain.RoleOwner {
		t.Fatalf("owner 成员行 = %+v", members)
	}
}

func TestCreateTenantWithOwnerRollsBackOnMemberFailure(t *testing.T) {
	t.Parallel()
	s, path := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)
	execRaw(t, path, `CREATE TRIGGER fail_owner BEFORE INSERT ON project_members
		WHEN NEW.tenant_id = 'team-broken'
		BEGIN SELECT RAISE(ABORT, 'injected owner failure'); END`)

	err := s.CreateTenantWithOwner(ctx, domain.Tenant{
		ID: "team-broken", UserID: "team-broken", Name: "team-broken", Kind: domain.TenantKindTeam,
	}, acct.ID, domain.RoleOwner)
	if err == nil {
		t.Fatal("成员写入失败应返回错误")
	}
	if _, err := s.GetTenant(ctx, "team-broken"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("回滚后不应残留租户行: %v", err)
	}
	members, err := s.ListMembers(ctx, "team-broken")
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	if len(members) != 0 {
		t.Fatalf("回滚后不应有成员行: %+v", members)
	}
}

func TestCreateTenantWithOwnerKeepsConflictSemantics(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)
	tenant := domain.Tenant{ID: "team-a", UserID: "team-a", Name: "team-a", Kind: domain.TenantKindTeam}

	if err := s.CreateTenantWithOwner(ctx, tenant, acct.ID, domain.RoleOwner); err != nil {
		t.Fatalf("first create: %v", err)
	}
	if err := s.CreateTenantWithOwner(ctx, tenant, acct.ID, domain.RoleOwner); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("同名创建 err = %v，期望 ErrConflict", err)
	}
}

func TestUpdateAccountAndRevokeSessionsCommitsBothRows(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)
	sess := createTestSession(t, s, acct.ID)

	next := acct
	next.Disabled = true
	next.PasswordHash = "new-hash"
	if err := s.UpdateAccountAndRevokeSessions(ctx, next, time.Now()); err != nil {
		t.Fatalf("UpdateAccountAndRevokeSessions: %v", err)
	}
	stored, err := s.GetAccount(ctx, acct.ID)
	if err != nil {
		t.Fatalf("GetAccount: %v", err)
	}
	if !stored.Disabled || stored.PasswordHash != "new-hash" {
		t.Fatalf("账号更新未落库: %+v", stored)
	}
	revoked, err := s.SessionByHash(ctx, sess.Hash)
	if err != nil {
		t.Fatalf("SessionByHash: %v", err)
	}
	if revoked.RevokedAt == nil {
		t.Fatal("会话应已被吊销")
	}
}

func TestUpdateAccountAndRevokeSessionsRollsBackOnRevokeFailure(t *testing.T) {
	t.Parallel()
	s, path := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)
	sess := createTestSession(t, s, acct.ID)
	execRaw(t, path, `CREATE TRIGGER fail_revoke BEFORE UPDATE ON sessions
		WHEN OLD.account_id = '`+acct.ID+`'
		BEGIN SELECT RAISE(ABORT, 'injected revoke failure'); END`)

	next := acct
	next.Disabled = true
	next.PasswordHash = "new-hash"
	if err := s.UpdateAccountAndRevokeSessions(ctx, next, time.Now()); err == nil {
		t.Fatal("吊销会话失败应返回错误")
	}
	stored, err := s.GetAccount(ctx, acct.ID)
	if err != nil {
		t.Fatalf("GetAccount: %v", err)
	}
	if stored.Disabled {
		t.Fatal("回滚后账号应保持启用")
	}
	if stored.PasswordHash != acct.PasswordHash {
		t.Fatalf("回滚后口令哈希应保持原值，得到 %q", stored.PasswordHash)
	}
	kept, err := s.SessionByHash(ctx, sess.Hash)
	if err != nil {
		t.Fatalf("SessionByHash: %v", err)
	}
	if kept.RevokedAt != nil {
		t.Fatal("回滚后会话不应被吊销")
	}
}

func createTestAccount(t *testing.T, s *store.Store) domain.Account {
	t.Helper()
	acct := domain.Account{
		ID:           "acct-alice",
		Username:     "alice",
		DisplayName:  "alice",
		PasswordHash: "hash-alice",
		CreatedAt:    time.Now(),
	}
	if err := s.CreateAccount(t.Context(), acct); err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	return acct
}

func createTestSession(t *testing.T, s *store.Store, accountID string) domain.Session {
	t.Helper()
	var hash [32]byte
	hash[0] = 1
	now := time.Now()
	sess := domain.Session{
		ID: "sess-" + accountID, Hash: hash, AccountID: accountID,
		CreatedAt: now, ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateSession(t.Context(), sess); err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	return sess
}
