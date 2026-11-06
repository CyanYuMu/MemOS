package domain

import (
	"errors"
	"testing"
)

func TestRoleCan(t *testing.T) {
	t.Parallel()

	caps := []Capability{
		CapProjectView, CapProjectWrite, CapProjectManage, CapProjectCreate, CapPlatformManage,
	}
	want := map[Role]map[Capability]bool{
		RoleViewer: {
			CapProjectView: true, CapProjectWrite: false, CapProjectManage: false,
			CapProjectCreate: true, CapPlatformManage: false,
		},
		RoleEditor: {
			CapProjectView: true, CapProjectWrite: true, CapProjectManage: false,
			CapProjectCreate: true, CapPlatformManage: false,
		},
		RoleOwner: {
			CapProjectView: true, CapProjectWrite: true, CapProjectManage: true,
			CapProjectCreate: true, CapPlatformManage: false,
		},
	}
	for role, row := range want {
		for _, c := range caps {
			if got := role.Can(c); got != row[c] {
				t.Errorf("%s.Can(%s) = %v, want %v", role, c, got, row[c])
			}
		}
	}
	for _, c := range caps {
		if Role("root").Can(c) {
			t.Errorf("非法角色不应具备 %s", c)
		}
	}
}

func TestPrincipalCan(t *testing.T) {
	t.Parallel()

	member := Principal{
		Kind: PrincipalAccount, AccountID: "a1",
		Roles: map[string]Role{"cube-a": RoleEditor},
	}
	if !member.Can("cube-a", CapProjectWrite) {
		t.Error("editor 应能写自己的项目")
	}
	if member.Can("cube-b", CapProjectView) {
		t.Error("非成员不应能看别人的项目")
	}
	if !member.Can("", CapProjectCreate) {
		t.Error("已登录账号应能建项目")
	}
	if member.Can("cube-a", CapPlatformManage) {
		t.Error("普通账号不应有平台能力")
	}

	token := Principal{Kind: PrincipalAdminToken, PlatformAdmin: true}
	if !token.Can("any", CapPlatformManage) || !token.Can("any", CapProjectManage) {
		t.Error("管理 Token 应等价于平台管理员")
	}
	if !token.Can("", CapProjectCreate) {
		t.Error("已认证主体应能调用建项目；管理 Token 的 ownerUsername 由 handler 强制")
	}

	admin := Principal{Kind: PrincipalAccount, AccountID: "a1", PlatformAdmin: true}
	for _, c := range []Capability{CapProjectView, CapProjectWrite, CapProjectManage, CapProjectCreate, CapPlatformManage} {
		if !admin.Can("any", c) {
			t.Errorf("平台管理员应具备 %s", c)
		}
	}

	if err := member.RequireCap("cube-b", CapProjectView); !errors.Is(err, ErrForbidden) {
		t.Errorf("非成员 RequireCap = %v, want ErrForbidden", err)
	}
	if err := member.RequireCap("cube-a", CapProjectWrite); err != nil {
		t.Errorf("editor RequireCap(write) = %v", err)
	}
}
