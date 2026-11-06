package tenant

import (
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 回归：运维 upsert 曾经只回读 Description 与 MultiView，store 的 upsert 又会写
// archived_at，结果对已归档项目 upsert 一次就把它悄悄恢复了。
func TestUpsertKeepsArchivedState(t *testing.T) {
	t.Parallel()
	archivedAt := time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC)
	store := newFakeStore(domain.Tenant{
		ID: "gone", UserID: "gone", Description: "keep", MultiView: true, Disabled: true, ArchivedAt: &archivedAt,
	})
	svc, calls := newService(store)

	got, err := svc.Upsert(t.Context(), UpsertInput{ID: "gone", UserID: "gone"})
	if err != nil {
		t.Fatalf("Upsert: %v", err)
	}
	if !got.Archived() || !got.ArchivedAt.Equal(archivedAt) || !got.Disabled {
		t.Fatalf("归档状态被改动: %+v", got)
	}
	if got.Description != "keep" || !got.MultiView {
		t.Fatalf("未提供的字段应保留原值: %+v", got)
	}
	if *calls != 1 {
		t.Fatalf("invalidate = %d", *calls)
	}
}

func TestUpsertCreateAndOverwrite(t *testing.T) {
	t.Parallel()
	store := newFakeStore()
	svc, _ := newService(store)

	created, err := svc.Upsert(t.Context(), UpsertInput{ID: "cube-b", UserID: "bob"})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if created.Name != "bob" {
		t.Fatalf("name 缺省应取 user_id，got %q", created.Name)
	}

	desc, multi := "new", true
	updated, err := svc.Upsert(t.Context(), UpsertInput{
		ID: "cube-b", UserID: "bob", Name: "B", Disabled: true, Description: &desc, MultiView: &multi,
	})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if updated.Name != "B" || !updated.Disabled || updated.Description != desc || !updated.MultiView {
		t.Fatalf("updated = %+v", updated)
	}

	// disabled 缺省为 false：与迁移前的整体覆盖语义一致。
	again, err := svc.Upsert(t.Context(), UpsertInput{ID: "cube-b", UserID: "bob"})
	if err != nil || again.Disabled || again.Description != desc {
		t.Fatalf("again = %+v err=%v", again, err)
	}
}

func TestUpsertRules(t *testing.T) {
	t.Parallel()
	personal := domain.Tenant{ID: "u-erin", UserID: "u-erin", Kind: domain.TenantKindPersonal}

	rejected := []struct {
		name string
		in   UpsertInput
	}{
		{"id 含空格", UpsertInput{ID: "bad id", UserID: "x"}},
		{"缺 user_id", UpsertInput{ID: "cube-b"}},
		{"新建项目不得占用个人项目保留前缀", UpsertInput{ID: "u-new", UserID: "u-new"}},
	}
	for _, tc := range rejected {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			store := newFakeStore(personal)
			svc, calls := newService(store)
			_, err := svc.Upsert(t.Context(), tc.in)
			wantKind(t, err, domain.ErrInvalidInput)
			if len(store.tenants) != 1 || *calls != 0 {
				t.Fatalf("被拒绝的 upsert 不得落库: tenants=%d calls=%d", len(store.tenants), *calls)
			}
		})
	}

	t.Run("已有个人项目仍可被平台管理员停用", func(t *testing.T) {
		t.Parallel()
		store := newFakeStore(personal)
		svc, _ := newService(store)
		got, err := svc.Upsert(t.Context(), UpsertInput{ID: "u-erin", UserID: "u-erin", Disabled: true})
		if err != nil {
			t.Fatalf("Upsert: %v", err)
		}
		if !got.Disabled || got.Kind != domain.TenantKindPersonal {
			t.Fatalf("got = %+v", got)
		}
	})
}
