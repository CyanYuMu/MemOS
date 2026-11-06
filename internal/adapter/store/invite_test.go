package store_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func seedInviteWorld(t *testing.T, s interface {
	UpsertTenant(context.Context, domain.Tenant) error
	CreateAccount(context.Context, domain.Account) error
},
) (accountID, tenantID string) {
	t.Helper()
	ctx := context.Background()
	accountID, tenantID = "acct-1", "cube-a"
	if err := s.UpsertTenant(ctx, domain.Tenant{ID: tenantID, UserID: "alice"}); err != nil {
		t.Fatalf("tenant: %v", err)
	}
	if err := s.CreateAccount(ctx, domain.Account{
		ID: accountID, Username: "alice", DisplayName: "Alice", CreatedAt: time.Now(),
	}); err != nil {
		t.Fatalf("account: %v", err)
	}
	return accountID, tenantID
}

func TestInviteLifecycle(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	accountID, tenantID := seedInviteWorld(t, s)

	raw := "inv_" + "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	now := time.Now().UTC().Truncate(time.Second)
	inv := domain.ProjectInvite{
		ID: "inv-1", TenantID: tenantID, Role: domain.RoleEditor,
		TokenHash: domain.HashInviteToken(raw), CreatedByAccountID: accountID,
		CreatedAt: now, ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateInvite(ctx, inv); err != nil {
		t.Fatalf("CreateInvite: %v", err)
	}
	got, err := s.GetInviteByHash(ctx, domain.HashInviteToken(raw))
	if err != nil || got.ID != inv.ID || got.Role != domain.RoleEditor {
		t.Fatalf("GetInviteByHash = %+v, %v", got, err)
	}

	if err := s.RedeemInvite(ctx, domain.RedeemInviteInput{
		InviteID: inv.ID, TenantID: tenantID, Role: domain.RoleEditor,
		AccountID: accountID, Now: now.Add(time.Minute),
	}); err != nil {
		t.Fatalf("RedeemInvite: %v", err)
	}
	used, err := s.GetInvite(ctx, inv.ID)
	if err != nil || used.UsedAt == nil || used.UsedByAccountID != accountID {
		t.Fatalf("used invite = %+v, %v", used, err)
	}
	acct, err := s.GetAccount(ctx, accountID)
	if err != nil || acct.CurrentTenantID != tenantID {
		t.Fatalf("account current tenant = %+v, %v", acct, err)
	}
	members, err := s.ListMembers(ctx, tenantID)
	if err != nil || len(members) != 1 || members[0].Role != domain.RoleEditor {
		t.Fatalf("members = %+v, %v", members, err)
	}

	// 第二次兑换被拒，成员关系不变。
	err = s.RedeemInvite(ctx, domain.RedeemInviteInput{
		InviteID: inv.ID, TenantID: tenantID, Role: domain.RoleEditor,
		AccountID: accountID, Now: now.Add(2 * time.Minute),
	})
	if !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("second redeem = %v, want ErrNotFound", err)
	}
}

func TestInviteExpiredAndRoleUpgrade(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	accountID, tenantID := seedInviteWorld(t, s)
	if err := s.PutMember(ctx, domain.Membership{
		AccountID: accountID, TenantID: tenantID, Role: domain.RoleOwner, CreatedAt: time.Now(),
	}); err != nil {
		t.Fatalf("PutMember: %v", err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	expired := domain.ProjectInvite{
		ID: "inv-exp", TenantID: tenantID, Role: domain.RoleViewer,
		TokenHash: domain.HashInviteToken("inv_expired"), CreatedAt: now.Add(-2 * time.Hour),
		ExpiresAt: now.Add(-time.Hour),
	}
	if err := s.CreateInvite(ctx, expired); err != nil {
		t.Fatalf("CreateInvite expired: %v", err)
	}
	err := s.RedeemInvite(ctx, domain.RedeemInviteInput{
		InviteID: expired.ID, TenantID: tenantID, Role: domain.RoleViewer,
		AccountID: accountID, Now: now,
	})
	if !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("expired redeem = %v, want ErrNotFound", err)
	}
	stored, err := s.GetInvite(ctx, expired.ID)
	if err != nil || stored.UsedAt != nil {
		t.Fatalf("expired invite must not be consumed: %+v, %v", stored, err)
	}

	// owner 兑换 viewer 邀请仍是 owner：角色只升不降。
	active := domain.ProjectInvite{
		ID: "inv-up", TenantID: tenantID, Role: domain.RoleViewer,
		TokenHash: domain.HashInviteToken("inv_upgrade"), CreatedAt: now,
		ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateInvite(ctx, active); err != nil {
		t.Fatalf("CreateInvite active: %v", err)
	}
	if err := s.RedeemInvite(ctx, domain.RedeemInviteInput{
		InviteID: active.ID, TenantID: tenantID, Role: domain.RoleViewer,
		AccountID: accountID, Now: now.Add(time.Minute),
	}); err != nil {
		t.Fatalf("Redeem upgrade: %v", err)
	}
	members, err := s.ListMembers(ctx, tenantID)
	if err != nil || len(members) != 1 || members[0].Role != domain.RoleOwner {
		t.Fatalf("owner must not be downgraded: %+v, %v", members, err)
	}
}

func TestInvitePurgeAndRevoke(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	accountID, tenantID := seedInviteWorld(t, s)
	now := time.Now().UTC().Truncate(time.Second)

	old := domain.ProjectInvite{
		ID: "inv-old", TenantID: tenantID, Role: domain.RoleViewer,
		TokenHash: domain.HashInviteToken("inv_old"), CreatedAt: now.Add(-60 * 24 * time.Hour),
		ExpiresAt: now.Add(-40 * 24 * time.Hour),
	}
	live := domain.ProjectInvite{
		ID: "inv-live", TenantID: tenantID, Role: domain.RoleViewer,
		TokenHash: domain.HashInviteToken("inv_live"), CreatedAt: now,
		ExpiresAt: now.Add(time.Hour),
	}
	for _, inv := range []domain.ProjectInvite{old, live} {
		if err := s.CreateInvite(ctx, inv); err != nil {
			t.Fatalf("CreateInvite %s: %v", inv.ID, err)
		}
	}
	n, err := s.PurgeInvitesBefore(ctx, now.Add(-30*24*time.Hour))
	if err != nil || n != 1 {
		t.Fatalf("Purge = %d, %v; want 1", n, err)
	}
	if _, err := s.GetInvite(ctx, live.ID); err != nil {
		t.Fatalf("live invite should survive: %v", err)
	}
	if err := s.RevokeInvite(ctx, tenantID, live.ID, now); err != nil {
		t.Fatalf("RevokeInvite: %v", err)
	}
	revoked, err := s.GetInvite(ctx, live.ID)
	if err != nil || revoked.RevokedAt == nil {
		t.Fatalf("revoked invite = %+v, %v", revoked, err)
	}
	if err := s.RevokeInvite(ctx, tenantID, "missing", now); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("revoke missing = %v", err)
	}
	_ = accountID
}

func TestSearchAccounts(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now()
	for _, a := range []domain.Account{
		{ID: "a1", Username: "alice", DisplayName: "爱丽丝", CreatedAt: now},
		{ID: "a2", Username: "alina", DisplayName: "Alina Zhang", CreatedAt: now},
		{ID: "a3", Username: "bob", DisplayName: "Bob Cooper", CreatedAt: now},
	} {
		if err := s.CreateAccount(ctx, a); err != nil {
			t.Fatalf("account %s: %v", a.ID, err)
		}
	}
	byPrefix, err := s.SearchAccounts(ctx, "@ali", 10)
	if err != nil || len(byPrefix) != 2 {
		t.Fatalf("prefix search = %+v, %v", byPrefix, err)
	}
	// 显示名子串也能命中：cooper 找到 bob，即便用户名不含它。
	byDisplay, err := s.SearchAccounts(ctx, "cooper", 10)
	if err != nil || len(byDisplay) != 1 || byDisplay[0].ID != "a3" {
		t.Fatalf("display search = %+v, %v", byDisplay, err)
	}
	if got, err := s.SearchAccounts(ctx, "  ", 10); err != nil || len(got) != 0 {
		t.Fatalf("empty query = %+v, %v", got, err)
	}
	if got, err := s.SearchAccounts(ctx, "100%", 10); err != nil || len(got) != 0 {
		t.Fatalf("wildcard should be literal: %+v, %v", got, err)
	}
}

// TestRedeemInviteConcurrent 证明并发兑换只有一条成功。
func TestRedeemInviteConcurrent(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	accountID, tenantID := seedInviteWorld(t, s)
	now := time.Now().UTC().Truncate(time.Second)
	inv := domain.ProjectInvite{
		ID: "inv-race", TenantID: tenantID, Role: domain.RoleEditor,
		TokenHash: domain.HashInviteToken("inv_race"), CreatedAt: now, ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateInvite(ctx, inv); err != nil {
		t.Fatalf("CreateInvite: %v", err)
	}

	const n = 4
	errs := make(chan error, n)
	for range n {
		go func() {
			errs <- s.RedeemInvite(ctx, domain.RedeemInviteInput{
				InviteID: inv.ID, TenantID: tenantID, Role: domain.RoleEditor,
				AccountID: accountID, Now: now.Add(time.Minute),
			})
		}()
	}
	ok := 0
	for range n {
		err := <-errs
		switch {
		case err == nil:
			ok++
		case !errors.Is(err, domain.ErrNotFound):
			t.Fatalf("unexpected err: %v", err)
		}
	}
	if ok != 1 {
		t.Fatalf("success count = %d, want 1", ok)
	}
}
