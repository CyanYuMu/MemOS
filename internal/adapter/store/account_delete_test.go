package store_test

import (
	"errors"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestDeleteAccountCleansPersonalAndMemberships(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()

	alice := createTestAccount(t, s)
	// 个人项目（含 owner 成员关系）
	personal, err := s.EnsurePersonalTenant(ctx, domain.Tenant{
		ID: "u-" + alice.ID, UserID: "u-" + alice.ID, Name: "alice 的空间",
		CreatedByAccountID: alice.ID,
	}, domain.Membership{AccountID: alice.ID, TenantID: "u-" + alice.ID, Role: domain.RoleOwner})
	if err != nil {
		t.Fatalf("EnsurePersonalTenant: %v", err)
	}
	// 团队项目里 alice 是成员；alice 建了一把团队 Key
	bob := domain.Account{ID: "acct-bob", Username: "bob", DisplayName: "bob", PasswordHash: "hash-bob"}
	if err := s.CreateAccount(ctx, bob); err != nil {
		t.Fatalf("CreateAccount bob: %v", err)
	}
	team := domain.Tenant{ID: "team-a", UserID: "team-a", Name: "team-a"}
	if err := s.CreateTenantWithOwner(ctx, team, bob.ID, domain.RoleOwner); err != nil {
		t.Fatalf("CreateTenantWithOwner: %v", err)
	}
	if err := s.PutMember(ctx, domain.Membership{AccountID: alice.ID, TenantID: "team-a", Role: domain.RoleEditor}); err != nil {
		t.Fatalf("PutMember: %v", err)
	}
	if err := s.CreateToken(ctx, domain.Token{
		ID: "tok-team", Kind: domain.TokenKindTenant, TenantID: "team-a", UserID: "team-a",
		Hash: domain.HashToken("team-key"), Prefix: "memos", Label: "ci", CreatedByAccountID: alice.ID,
	}); err != nil {
		t.Fatalf("CreateToken: %v", err)
	}
	sess := createTestSession(t, s, alice.ID)
	if err := s.SetSessionTenant(ctx, sess.ID, personal.ID); err != nil {
		t.Fatalf("SetSessionTenant: %v", err)
	}

	if err := s.DeleteAccount(ctx, alice.ID); err != nil {
		t.Fatalf("DeleteAccount: %v", err)
	}
	if _, err := s.GetAccount(ctx, alice.ID); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("账号应已删除, err=%v", err)
	}
	if _, err := s.GetTenant(ctx, personal.ID); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("个人项目应随账号删除, err=%v", err)
	}
	members, err := s.ListMembers(ctx, team.ID)
	if err != nil {
		t.Fatalf("ListMembers: %v", err)
	}
	for _, m := range members {
		if m.AccountID == alice.ID {
			t.Fatalf("团队项目里的成员关系应删除: %v", members)
		}
	}
	if sessions, err := s.ListSessions(ctx, alice.ID); err != nil || len(sessions) != 0 {
		t.Fatalf("会话应删除: %v %v", sessions, err)
	}
	tokens, err := s.ListTokens(ctx, team.ID)
	if err != nil {
		t.Fatalf("ListTokens: %v", err)
	}
	if len(tokens) != 1 {
		t.Fatalf("团队 Key 不应被删: %v", tokens)
	}
	if tokens[0].CreatedByAccountID != "" {
		t.Fatalf("团队 Key 的创建者引用应清空, got %q", tokens[0].CreatedByAccountID)
	}
	if err := s.DeleteAccount(ctx, alice.ID); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("重复删除应 NotFound, err=%v", err)
	}
}
