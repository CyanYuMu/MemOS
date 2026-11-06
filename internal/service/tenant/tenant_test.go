package tenant

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type fakeStore struct {
	tenants   map[string]domain.Tenant
	owners    map[string]string
	createErr error
}

func newFakeStore(seed ...domain.Tenant) *fakeStore {
	f := &fakeStore{tenants: map[string]domain.Tenant{}, owners: map[string]string{}}
	for i := range seed {
		f.tenants[seed[i].ID] = seed[i]
	}
	return f
}

func (f *fakeStore) GetTenant(_ context.Context, id string) (domain.Tenant, error) {
	t, ok := f.tenants[id]
	if !ok {
		return domain.Tenant{}, domain.NotFound("get_tenant", "租户不存在")
	}
	return t, nil
}

func (f *fakeStore) ListTenants(context.Context) ([]domain.Tenant, error) {
	out := make([]domain.Tenant, 0, len(f.tenants))
	for id := range f.tenants {
		out = append(out, f.tenants[id])
	}
	return out, nil
}

// UpsertTenant 与真实 store 一致：归档项目强制停用，kind 与创建者不被覆盖。
func (f *fakeStore) UpsertTenant(_ context.Context, t domain.Tenant) error {
	if existing, ok := f.tenants[t.ID]; ok {
		t.Kind, t.CreatedByAccountID, t.CreatedAt = existing.Kind, existing.CreatedByAccountID, existing.CreatedAt
	}
	if t.Archived() {
		t.Disabled = true
	}
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) CreateTenantWithOwner(_ context.Context, t domain.Tenant, owner string, _ domain.Role) error {
	if f.createErr != nil {
		return f.createErr
	}
	f.tenants[t.ID] = t
	f.owners[t.ID] = owner
	return nil
}

func (f *fakeStore) ArchiveTenant(_ context.Context, id string, at time.Time) error {
	t := f.tenants[id]
	t.ArchivedAt, t.Disabled = &at, true
	f.tenants[id] = t
	return nil
}

func (f *fakeStore) RestoreTenant(_ context.Context, id string) error {
	t := f.tenants[id]
	t.ArchivedAt, t.Disabled = nil, false
	f.tenants[id] = t
	return nil
}

type fakeAccounts map[string]domain.Account

func (f fakeAccounts) ResolveAccount(_ context.Context, ref string) (domain.Account, error) {
	a, ok := f[ref]
	if !ok {
		return domain.Account{}, domain.NotFound("resolve_account", "账号不存在")
	}
	return a, nil
}

func newService(store *fakeStore) (*Service, *int) {
	calls := new(int)
	accounts := fakeAccounts{
		"alice": {ID: "acct-alice"},
		"gone":  {ID: "acct-gone", Disabled: true},
	}
	return New(store, accounts, func() { *calls++ }), calls
}

func wantKind(t *testing.T, err, kind error) {
	t.Helper()
	if !errors.Is(err, kind) {
		t.Fatalf("err = %v，期望 %v", err, kind)
	}
}

func TestCreate(t *testing.T) {
	t.Parallel()
	member := domain.Principal{Kind: domain.PrincipalAccount, AccountID: "acct-bob"}
	admin := domain.Principal{Kind: domain.PrincipalAdminToken}

	rejected := []struct {
		name  string
		actor domain.Principal
		in    CreateInput
	}{
		{"名称为空", member, CreateInput{Name: "  "}},
		{"含非法字符", member, CreateInput{Name: "bad name"}},
		{"超过 64 位", member, CreateInput{Name: string(make([]byte, 65))}},
		{"落在个人项目保留前缀", member, CreateInput{Name: "u-anything"}},
		{"同名项目已存在", member, CreateInput{Name: "team-a"}},
		{"管理 Token 缺 owner", admin, CreateInput{Name: "ops"}},
		{"owner 不存在", admin, CreateInput{Name: "ops", OwnerUsername: "nobody"}},
		{"owner 已禁用", admin, CreateInput{Name: "ops", OwnerUsername: "gone"}},
	}
	for _, tc := range rejected {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			store := newFakeStore(domain.Tenant{ID: "team-a", UserID: "team-a"})
			svc, calls := newService(store)
			_, err := svc.Create(t.Context(), tc.actor, tc.in)
			if err == nil {
				t.Fatal("期望被拒绝")
			}
			if len(store.owners) != 0 || *calls != 0 {
				t.Fatalf("被拒绝的创建不得落库或清缓存：owners=%v calls=%d", store.owners, *calls)
			}
		})
	}

	t.Run("账号主体自己成为 owner", func(t *testing.T) {
		t.Parallel()
		store := newFakeStore()
		svc, calls := newService(store)
		got, err := svc.Create(t.Context(), member, CreateInput{Name: " team-b ", Description: "d", MultiView: true})
		if err != nil {
			t.Fatalf("Create: %v", err)
		}
		if got.ID != "team-b" || got.UserID != "team-b" || got.Kind != domain.TenantKindTeam || !got.MultiView || got.Description != "d" {
			t.Fatalf("created = %+v", got)
		}
		if store.owners["team-b"] != "acct-bob" || got.CreatedByAccountID != "acct-bob" {
			t.Fatalf("owner = %q，创建者 = %q", store.owners["team-b"], got.CreatedByAccountID)
		}
		if *calls != 1 {
			t.Fatalf("invalidate 调用次数 = %d，期望 1", *calls)
		}
	})

	t.Run("管理 Token 指定 owner", func(t *testing.T) {
		t.Parallel()
		store := newFakeStore()
		svc, _ := newService(store)
		if _, err := svc.Create(t.Context(), admin, CreateInput{Name: "ops", OwnerUsername: " alice "}); err != nil {
			t.Fatalf("Create: %v", err)
		}
		if store.owners["ops"] != "acct-alice" {
			t.Fatalf("owner = %q，期望 acct-alice", store.owners["ops"])
		}
	})

	t.Run("预检查之后的并发同名创建映射为参数错误", func(t *testing.T) {
		t.Parallel()
		store := newFakeStore()
		store.createErr = domain.Wrap(domain.ErrConflict, "create_tenant_with_owner", "已存在同名项目", nil)
		svc, calls := newService(store)
		_, err := svc.Create(t.Context(), member, CreateInput{Name: "race"})
		wantKind(t, err, domain.ErrInvalidInput)
		if *calls != 0 {
			t.Fatalf("失败的创建不得清缓存，calls=%d", *calls)
		}
	})
}

func TestUpdate(t *testing.T) {
	t.Parallel()
	archivedAt := time.Now()
	seed := []domain.Tenant{
		{ID: "team-a", UserID: "team-a", Name: "old"},
		{ID: "u-bob", UserID: "u-bob", Kind: domain.TenantKindPersonal, CreatedByAccountID: "acct-bob"},
		{ID: "gone", UserID: "gone", Disabled: true, ArchivedAt: &archivedAt},
	}
	yes, no, name, bad := true, false, "renamed", "bad name"

	t.Run("字段按补丁生效并清缓存", func(t *testing.T) {
		t.Parallel()
		svc, calls := newService(newFakeStore(seed...))
		desc := "d"
		got, err := svc.Update(t.Context(), "team-a", Patch{Name: &name, Description: &desc, MultiView: &yes, Disabled: &yes})
		if err != nil {
			t.Fatalf("Update: %v", err)
		}
		if got.Name != name || got.Description != desc || !got.MultiView || !got.Disabled {
			t.Fatalf("updated = %+v", got)
		}
		if *calls != 1 {
			t.Fatalf("invalidate = %d", *calls)
		}
	})

	t.Run("nil 字段不动", func(t *testing.T) {
		t.Parallel()
		svc, _ := newService(newFakeStore(seed...))
		got, err := svc.Update(t.Context(), "team-a", Patch{Disabled: &no})
		if err != nil || got.Name != "old" {
			t.Fatalf("got=%+v err=%v", got, err)
		}
	})

	rejected := []struct {
		name  string
		id    string
		patch Patch
		kind  error
	}{
		{"归档项目只能先恢复", "gone", Patch{Disabled: &no}, domain.ErrConflict},
		{"个人项目不能停用", "u-bob", Patch{Disabled: &yes}, domain.ErrConflict},
		{"名称格式非法", "team-a", Patch{Name: &bad}, domain.ErrInvalidInput},
		{"项目不存在", "nope", Patch{Name: &name}, domain.ErrNotFound},
	}
	for _, tc := range rejected {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			svc, calls := newService(newFakeStore(seed...))
			_, err := svc.Update(t.Context(), tc.id, tc.patch)
			wantKind(t, err, tc.kind)
			if *calls != 0 {
				t.Fatalf("被拒绝的修改不得清缓存，calls=%d", *calls)
			}
		})
	}

	t.Run("个人项目可以改描述", func(t *testing.T) {
		t.Parallel()
		svc, _ := newService(newFakeStore(seed...))
		desc := "mine"
		if _, err := svc.Update(t.Context(), "u-bob", Patch{Description: &desc}); err != nil {
			t.Fatalf("Update: %v", err)
		}
	})
}

func TestDisableArchiveRestore(t *testing.T) {
	t.Parallel()
	seed := func() *fakeStore {
		return newFakeStore(
			domain.Tenant{ID: "team-a", UserID: "team-a"},
			domain.Tenant{ID: "u-bob", UserID: "u-bob", Kind: domain.TenantKindPersonal},
		)
	}

	t.Run("停用团队项目", func(t *testing.T) {
		t.Parallel()
		store := seed()
		svc, calls := newService(store)
		if err := svc.Disable(t.Context(), "team-a"); err != nil {
			t.Fatalf("Disable: %v", err)
		}
		if !store.tenants["team-a"].Disabled || *calls != 1 {
			t.Fatalf("disabled=%v calls=%d", store.tenants["team-a"].Disabled, *calls)
		}
	})

	t.Run("个人项目不能停用也不能归档", func(t *testing.T) {
		t.Parallel()
		svc, calls := newService(seed())
		wantKind(t, svc.Disable(t.Context(), "u-bob"), domain.ErrConflict)
		wantKind(t, svc.Archive(t.Context(), "u-bob"), domain.ErrConflict)
		if *calls != 0 {
			t.Fatalf("calls=%d", *calls)
		}
	})

	t.Run("归档后恢复", func(t *testing.T) {
		t.Parallel()
		store := seed()
		svc, calls := newService(store)
		fixed := time.Date(2026, 10, 26, 8, 0, 0, 0, time.UTC)
		svc.now = func() time.Time { return fixed }
		if err := svc.Archive(t.Context(), "team-a"); err != nil {
			t.Fatalf("Archive: %v", err)
		}
		got := store.tenants["team-a"]
		if !got.Archived() || !got.ArchivedAt.Equal(fixed) || !got.Disabled {
			t.Fatalf("archived = %+v", got)
		}
		if err := svc.Restore(t.Context(), "team-a"); err != nil {
			t.Fatalf("Restore: %v", err)
		}
		if got := store.tenants["team-a"]; got.Archived() || got.Disabled {
			t.Fatalf("restored = %+v", got)
		}
		if *calls != 2 {
			t.Fatalf("calls=%d，期望 2", *calls)
		}
	})

	t.Run("恢复未归档的项目是冲突", func(t *testing.T) {
		t.Parallel()
		svc, calls := newService(seed())
		wantKind(t, svc.Restore(t.Context(), "team-a"), domain.ErrConflict)
		if *calls != 0 {
			t.Fatalf("calls=%d", *calls)
		}
	})

	t.Run("项目不存在", func(t *testing.T) {
		t.Parallel()
		svc, _ := newService(seed())
		wantKind(t, svc.Disable(t.Context(), "nope"), domain.ErrNotFound)
		wantKind(t, svc.Archive(t.Context(), "nope"), domain.ErrNotFound)
		wantKind(t, svc.Restore(t.Context(), "nope"), domain.ErrNotFound)
	})
}
