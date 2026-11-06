package store_test

import (
	"errors"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestUpdateTokenLabel(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)
	if err := s.CreateTenantWithOwner(ctx,
		domain.Tenant{ID: "cube-a", UserID: "cube-a", Name: "cube-a"}, acct.ID, domain.RoleOwner); err != nil {
		t.Fatalf("CreateTenantWithOwner: %v", err)
	}

	tok := domain.Token{
		ID: "tok-rename", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "cube-a",
		Hash: domain.HashToken("rename-key"), Prefix: "memos", Label: "old",
	}
	if err := s.CreateToken(ctx, tok); err != nil {
		t.Fatalf("CreateToken: %v", err)
	}
	if err := s.UpdateTokenLabel(ctx, tok.ID, "new"); err != nil {
		t.Fatalf("UpdateTokenLabel: %v", err)
	}
	got, err := s.GetToken(ctx, tok.ID)
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}
	if got.Label != "new" || got.Hash != tok.Hash {
		t.Fatalf("只应改名，哈希不变: %+v", got)
	}
	if err := s.UpdateTokenLabel(ctx, "missing", "x"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("不存在的 Key 应 404: %v", err)
	}
}

func TestUpdateInviteRole(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()
	now := time.Now()
	acct := createTestAccount(t, s)
	if err := s.CreateTenantWithOwner(ctx,
		domain.Tenant{ID: "cube-a", UserID: "cube-a", Name: "cube-a"}, acct.ID, domain.RoleOwner); err != nil {
		t.Fatalf("CreateTenantWithOwner: %v", err)
	}

	inv := domain.ProjectInvite{
		ID: "inv-role", TenantID: "cube-a", Role: domain.RoleViewer,
		TokenHash: domain.HashToken("role-token"), CreatedAt: now, ExpiresAt: now.Add(time.Hour),
	}
	if err := s.CreateInvite(ctx, inv); err != nil {
		t.Fatalf("CreateInvite: %v", err)
	}
	if err := s.UpdateInviteRole(ctx, "cube-a", inv.ID, domain.RoleEditor); err != nil {
		t.Fatalf("UpdateInviteRole: %v", err)
	}
	got, err := s.GetInvite(ctx, inv.ID)
	if err != nil || got.Role != domain.RoleEditor {
		t.Fatalf("角色应更新: %+v %v", got, err)
	}
	if err := s.UpdateInviteRole(ctx, "cube-a", inv.ID, "root"); err == nil {
		t.Fatal("非法角色应被拒绝")
	}
	if err := s.UpdateInviteRole(ctx, "cube-b", inv.ID, domain.RoleOwner); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("跨项目改角色应 404: %v", err)
	}
}
