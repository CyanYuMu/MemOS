package store_test

import (
	"errors"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestArchiveAndRestoreTenant(t *testing.T) {
	t.Parallel()
	s, _ := openStoreWithPath(t)
	ctx := t.Context()
	acct := createTestAccount(t, s)

	const tenantID = "team-archive"
	tenant := domain.Tenant{ID: tenantID, UserID: tenantID, Name: tenantID, Kind: domain.TenantKindTeam}
	if err := s.CreateTenantWithOwner(ctx, tenant, acct.ID, domain.RoleOwner); err != nil {
		t.Fatalf("CreateTenantWithOwner: %v", err)
	}

	if err := s.ArchiveTenant(ctx, tenantID, time.Now()); err != nil {
		t.Fatalf("ArchiveTenant: %v", err)
	}
	archived, err := s.GetTenant(ctx, tenantID)
	if err != nil {
		t.Fatalf("GetTenant: %v", err)
	}
	if !archived.Archived() || !archived.Disabled {
		t.Fatalf("归档后应同时置 disabled: %+v", archived)
	}
	if members, err := s.ListMembers(ctx, tenantID); err != nil || len(members) != 1 {
		t.Fatalf("归档保留成员关系: %v %v", members, err)
	}

	if err := s.ArchiveTenant(ctx, tenantID, time.Now().Add(time.Hour)); err != nil {
		t.Fatalf("重复归档应幂等: %v", err)
	}
	again, _ := s.GetTenant(ctx, tenantID)
	if !again.ArchivedAt.Equal(*archived.ArchivedAt) {
		t.Fatalf("重复归档不应改写归档时间: %v != %v", again.ArchivedAt, archived.ArchivedAt)
	}

	if err := s.RestoreTenant(ctx, tenantID); err != nil {
		t.Fatalf("RestoreTenant: %v", err)
	}
	restored, err := s.GetTenant(ctx, tenantID)
	if err != nil {
		t.Fatalf("GetTenant: %v", err)
	}
	if restored.Archived() || restored.Disabled {
		t.Fatalf("恢复后应可用: %+v", restored)
	}
	if err := s.RestoreTenant(ctx, tenantID); err != nil {
		t.Fatalf("重复恢复应幂等: %v", err)
	}

	if err := s.ArchiveTenant(ctx, "missing", time.Now()); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("不存在的项目应 404: %v", err)
	}
}
