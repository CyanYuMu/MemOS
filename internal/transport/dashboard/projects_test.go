package dashboard

import (
	"context"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
)

func TestSwitchStatusUsesCurrentTenantForAccountPrincipals(t *testing.T) {
	cases := []struct {
		name      string
		principal domain.Principal
		tenant    domain.Tenant
		want      string
	}{
		{
			name:      "外部身份没有会话也按账号当前项目标记",
			principal: domain.Principal{Kind: domain.PrincipalAccount, CurrentTenant: "cube-a"},
			tenant:    domain.Tenant{ID: "cube-a"},
			want:      "ENABLED",
		},
		{
			name:      "非当前项目返回 DISABLED",
			principal: domain.Principal{Kind: domain.PrincipalAccount, CurrentTenant: "cube-a"},
			tenant:    domain.Tenant{ID: "cube-b"},
			want:      "DISABLED",
		},
		{
			name:      "停用的当前项目不能标成 ENABLED",
			principal: domain.Principal{Kind: domain.PrincipalAccount, CurrentTenant: "cube-a"},
			tenant:    domain.Tenant{ID: "cube-a", Disabled: true},
			want:      "DISABLED",
		},
		{
			name:      "管理 Token 沿用项目自身状态",
			principal: domain.Principal{Kind: domain.PrincipalAdminToken},
			tenant:    domain.Tenant{ID: "cube-a"},
			want:      "ENABLED",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := switchStatus(tc.principal, tc.tenant); got != tc.want {
				t.Fatalf("switchStatus() = %q, want %q", got, tc.want)
			}
		})
	}
}

// fakeMemberRoles 只实现 visibleProjects 需要的真实成员关系；其余账号能力
// 沿用 adminOnly 的空实现。
type fakeMemberRoles struct {
	adminOnly
	roles map[string]domain.Role
}

func (f fakeMemberRoles) MemberRoles(context.Context, string) (map[string]domain.Role, error) {
	return f.roles, nil
}

// 个人空间不是项目：只有本人和真实成员看得到，平台管理员非成员也不该在
// 「项目与成员」里看到别人的个人空间（它既不该被停用，也不该把账号列表摊出来）。
func TestVisibleProjectsHidesOtherPersonalSpaces(t *testing.T) {
	store := newFakeStore()
	store.tenants["u-alice"] = domain.Tenant{
		ID: "u-alice", Name: "Alice 的空间", Kind: domain.TenantKindPersonal, CreatedByAccountID: "acct-alice",
	}
	store.tenants["u-bob"] = domain.Tenant{
		ID: "u-bob", Name: "Bob 的空间", Kind: domain.TenantKindPersonal, CreatedByAccountID: "acct-bob",
	}
	store.tenants["team-a"] = domain.Tenant{ID: "team-a", Name: "team-a"}
	srv := &Server{deps: Deps{
		Tenants:  tenant.New(store, nil, nil),
		Accounts: fakeMemberRoles{roles: map[string]domain.Role{"u-alice": domain.RoleOwner, "team-a": domain.RoleOwner}},
	}}

	admin := domain.Principal{
		Kind:          domain.PrincipalAccount,
		AccountID:     "acct-alice",
		PlatformAdmin: true,
		Roles:         map[string]domain.Role{"team-a": domain.RoleOwner},
	}
	got, _, err := srv.visibleProjects(t.Context(), admin, true)
	if err != nil {
		t.Fatalf("visibleProjects: %v", err)
	}
	ids := make(map[string]bool, len(got))
	for _, tenant := range got {
		ids[tenant.ID] = true
	}
	if !ids["u-alice"] || !ids["team-a"] {
		t.Fatalf("自己的个人空间与团队项目应可见: %v", ids)
	}
	if ids["u-bob"] {
		t.Fatalf("不应看到别人的个人空间: %v", ids)
	}
}
