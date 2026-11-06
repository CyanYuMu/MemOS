package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 空串与 NULL 都表示“没有时间值”（scan 层就是这么读的）。邀请的撤销/兑换条件
// 必须用同一口径，否则 revoked_at 为空串的邀请既不能兑换也不能被撤销。
func TestInviteEmptyTimestampsTreatedAsAbsent(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	s, err := Open(ctx, filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })

	now := time.Now().UTC().Truncate(time.Millisecond)
	if err := s.UpsertTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "alice", CreatedAt: now}); err != nil {
		t.Fatalf("UpsertTenant: %v", err)
	}
	if err := s.CreateAccount(ctx, domain.Account{ID: "acct-1", Username: "alice", CreatedAt: now}); err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}

	newInvite := func(id, token string) domain.ProjectInvite {
		inv := domain.ProjectInvite{
			ID: id, TenantID: "cube-a", Role: domain.RoleEditor,
			TokenHash: domain.HashInviteToken(token), CreatedByAccountID: "acct-1",
			CreatedAt: now, ExpiresAt: now.Add(time.Hour),
		}
		if err := s.CreateInvite(ctx, inv); err != nil {
			t.Fatalf("CreateInvite %s: %v", id, err)
		}
		return inv
	}

	// revoked_at 为空串：视为未撤销，仍可被撤销。
	revocable := newInvite("inv-revoke", "inv_revoke_token_0123456789abcdef0123456789abcdef")
	if _, err := s.db.ExecContext(ctx, `UPDATE project_invites SET revoked_at = '' WHERE id = ?`, revocable.ID); err != nil {
		t.Fatalf("set empty revoked_at: %v", err)
	}
	if err := s.RevokeInvite(ctx, revocable.TenantID, revocable.ID, now.Add(time.Minute)); err != nil {
		t.Fatalf("空串撤销时间应视为未撤销: %v", err)
	}
	if got, err := s.GetInvite(ctx, revocable.ID); err != nil || got.RevokedAt == nil {
		t.Fatalf("撤销后 revoked_at = %+v, %v", got.RevokedAt, err)
	}

	// used_at 为空串：视为未使用，仍可兑换。
	redeemable := newInvite("inv-redeem", "inv_redeem_token_0123456789abcdef0123456789abcdef")
	if _, err := s.db.ExecContext(ctx, `UPDATE project_invites SET used_at = '' WHERE id = ?`, redeemable.ID); err != nil {
		t.Fatalf("set empty used_at: %v", err)
	}
	if err := s.RedeemInvite(ctx, domain.RedeemInviteInput{
		InviteID: redeemable.ID, TenantID: redeemable.TenantID, Role: domain.RoleEditor,
		AccountID: "acct-1", Now: now.Add(time.Minute),
	}); err != nil {
		t.Fatalf("空串使用时间应视为未使用: %v", err)
	}
	if got, err := s.GetInvite(ctx, redeemable.ID); err != nil || got.UsedAt == nil {
		t.Fatalf("兑换后 used_at = %+v, %v", got.UsedAt, err)
	}
}
