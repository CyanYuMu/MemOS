package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 历史行与部分写入路径会把 revoked_at 写成空串而不是 NULL。空串必须和 NULL
// 一样视为“未吊销”，否则活跃会话既不续期、也不出现在会话列表里。
func TestSessionEmptyRevokedAtTreatedAsActive(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	s, err := Open(ctx, filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })

	now := time.Now().UTC().Truncate(time.Millisecond)
	if err := s.CreateAccount(ctx, domain.Account{ID: "acct-1", Username: "alice", CreatedAt: now}); err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	sess := domain.Session{
		Hash: [32]byte{1}, ID: "sess-empty-revoked", AccountID: "acct-1",
		CreatedAt: now, ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateSession(ctx, sess); err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	if _, err := s.db.ExecContext(ctx, `UPDATE sessions SET revoked_at = '' WHERE id = ?`, sess.ID); err != nil {
		t.Fatalf("set empty revoked_at: %v", err)
	}

	got, err := s.GetSession(ctx, sess.ID)
	if err != nil {
		t.Fatalf("GetSession: %v", err)
	}
	if got.RevokedAt != nil {
		t.Fatalf("空串吊销时间应读成未吊销: %+v", got.RevokedAt)
	}

	lastSeen := now.Add(time.Minute)
	extended := now.Add(2 * time.Hour)
	if err := s.ExtendSession(ctx, sess.ID, lastSeen, extended); err != nil {
		t.Fatalf("ExtendSession: %v", err)
	}
	if got, err = s.GetSession(ctx, sess.ID); err != nil {
		t.Fatalf("GetSession after extend: %v", err)
	}
	if !got.ExpiresAt.Equal(extended) || got.LastSeenAt == nil || !got.LastSeenAt.Equal(lastSeen) {
		t.Fatalf("空串吊销的会话应可续期: expires=%v lastSeen=%v", got.ExpiresAt, got.LastSeenAt)
	}

	list, err := s.ListSessions(ctx, sess.AccountID)
	if err != nil {
		t.Fatalf("ListSessions: %v", err)
	}
	if len(list) != 1 || list[0].ID != sess.ID {
		t.Fatalf("空串吊销的会话应出现在列表里: %+v", list)
	}

	// 真正吊销后（写入时间）不能再续期，也不能再出现在列表里。
	// 管理员重置口令/禁用账号走 UpdateAccountAndRevokeSessions，同样要认空串。
	acct, err := s.GetAccount(ctx, sess.AccountID)
	if err != nil {
		t.Fatalf("GetAccount: %v", err)
	}
	acct.DisplayName = "alice-updated"
	if err := s.UpdateAccountAndRevokeSessions(ctx, acct, now.Add(2*time.Minute)); err != nil {
		t.Fatalf("UpdateAccountAndRevokeSessions: %v", err)
	}
	if got, err = s.GetSession(ctx, sess.ID); err != nil {
		t.Fatalf("GetSession revoked: %v", err)
	}
	if got.RevokedAt == nil {
		t.Fatal("重置口令/禁用账号的路径也必须吊销空串吊销时间的会话")
	}
	if err := s.ExtendSession(ctx, sess.ID, lastSeen.Add(time.Minute), now.Add(3*time.Hour)); err != nil {
		t.Fatalf("ExtendSession revoked: %v", err)
	}
	if got, err = s.GetSession(ctx, sess.ID); err != nil {
		t.Fatalf("GetSession revoked: %v", err)
	}
	if !got.ExpiresAt.Equal(extended) {
		t.Fatalf("已吊销会话不应被续期: %v", got.ExpiresAt)
	}
	if list, err = s.ListSessions(ctx, sess.AccountID); err != nil {
		t.Fatalf("ListSessions revoked: %v", err)
	} else if len(list) != 0 {
		t.Fatalf("已吊销会话不应出现在列表里: %+v", list)
	}

	// RevokeSession 也走同一口径，单独再覆盖一次（事务路径是另一条 SQL）。
	other := domain.Session{
		Hash: [32]byte{2}, ID: "sess-empty-revoked-2", AccountID: sess.AccountID,
		CreatedAt: now, ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateSession(ctx, other); err != nil {
		t.Fatalf("CreateSession other: %v", err)
	}
	if _, err := s.db.ExecContext(ctx, `UPDATE sessions SET revoked_at = '' WHERE id = ?`, other.ID); err != nil {
		t.Fatalf("set empty revoked_at (other): %v", err)
	}
	if err := s.RevokeSession(ctx, other.ID, now.Add(3*time.Minute)); err != nil {
		t.Fatalf("RevokeSession: %v", err)
	}
	if got, err = s.GetSession(ctx, other.ID); err != nil {
		t.Fatalf("GetSession other: %v", err)
	}
	if got.RevokedAt == nil {
		t.Fatal("RevokeSession 也应吊销空串吊销时间的会话")
	}
}
