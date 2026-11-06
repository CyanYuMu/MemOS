package app

import (
	"fmt"
	"net/http"
	"testing"
)

// TestE2EPersonalProjectLifecycle 覆盖账号创建即拥有个人项目、默认当前项目指向它，
// 以及 owner 不能停用自己的个人项目。
func TestE2EPersonalProjectLifecycle(t *testing.T) {
	t.Parallel()
	e := newE2E(t)

	admin := e.adminClient()
	created := admin.post("/admin/accounts", `{"username":"erin","password":"pass-erin-12345","display_name":"Erin"}`)
	expect(t, "create erin", created, http.StatusOK)
	accountID := created.str("id")
	personalID := "u-" + accountID

	erin := e.client()
	expect(t, "login erin", erin.login("erin", "pass-erin-12345"), http.StatusOK)

	me := erin.get("/api/auth/me")
	expect(t, "me", me, http.StatusOK)
	if me.str("accountId") != accountID || me.str("currentProjectId") != personalID {
		t.Fatalf("me 应带 accountId 并默认落在个人项目: %s", me.raw)
	}
	var personal map[string]any
	projects, _ := me.obj()["projects"].([]any)
	for _, raw := range projects {
		if p, ok := raw.(map[string]any); ok && p["id"] == personalID {
			personal = p
		}
	}
	if personal == nil || personal["kind"] != "personal" || personal["role"] != "owner" || personal["name"] != "Erin 的空间" {
		t.Fatalf("个人项目字段不对: %s", me.raw)
	}

	dict := erin.get("/api/dashboard/project/dict")
	expect(t, "dict", dict, http.StatusOK)
	if got := dict.list(); len(got) != 1 || got[0]["id"] != personalID || got[0]["kind"] != "personal" || got[0]["status"] != "ENABLED" {
		t.Fatalf("新账号应只看到自己的个人项目: %s", dict.raw)
	}

	expect(t, "owner cannot disable personal",
		erin.post("/api/dashboard/project/update", `{"id":"`+personalID+`","status":"DISABLED"}`), http.StatusConflict)
	expect(t, "owner cannot revoke personal",
		erin.post("/api/dashboard/project/revoke/"+personalID, ""), http.StatusConflict)
	expect(t, "personal still current",
		erin.post("/api/dashboard/project/update", `{"id":"`+personalID+`","current":true}`), http.StatusOK)

	// 平台管理员仍可经 /admin/tenants 停用个人项目。
	expect(t, "platform admin disables personal via admin",
		admin.post("/admin/tenants", `{"id":"`+personalID+`","user_id":"`+personalID+`","name":"Erin 的空间","disabled":true}`),
		http.StatusOK)
}

// TestE2EAnyAccountCreatesProject 验证任意已登录账号都能建项目并自动成为 owner。
func TestE2EAnyAccountCreatesProject(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	alice := e.loginAs("alice") // cube-a 的 viewer

	created := alice.post("/api/dashboard/project/create", `{"name":"alice-team","description":"mine"}`)
	expect(t, "viewer creates project", created, http.StatusOK)
	if created.str("id") != "alice-team" || created.str("kind") != "team" {
		t.Fatalf("新建项目字段不对: %s", created.raw)
	}

	members := alice.get("/admin/tenants/alice-team/members")
	expect(t, "creator lists members", members, http.StatusOK)
	if records := members.records(); len(records) != 1 || records[0]["username"] != "alice" || records[0]["role"] != "owner" {
		t.Fatalf("创建者应自动成为 owner: %s", members.raw)
	}

	expect(t, "reserved u- prefix",
		alice.post("/api/dashboard/project/create", `{"name":"u-anything"}`), http.StatusBadRequest)

	// 管理 Token 没有账号身份，必须显式指定 owner。
	admin := e.adminClient()
	expect(t, "admin token needs owner",
		admin.post("/api/dashboard/project/create", `{"name":"ops-team"}`), http.StatusBadRequest)
	expect(t, "admin token with owner",
		admin.post("/api/dashboard/project/create", fmt.Sprintf(`{"name":"ops-team","ownerUsername":%q}`, "alice")),
		http.StatusOK)
	members = admin.get("/admin/tenants/ops-team/members")
	expect(t, "ops team members", members, http.StatusOK)
	if records := members.records(); len(records) != 1 || records[0]["account_id"] != ids["alice"] || records[0]["role"] != "owner" {
		t.Fatalf("ownerUsername 指定的账号应成为 owner: %s", members.raw)
	}
}

// TestE2EPlatformAdminCannotSelfDemoteOrDisable 验证平台管理员的自我保护。
func TestE2EPlatformAdminCannotSelfDemoteOrDisable(t *testing.T) {
	t.Parallel()
	e := newE2E(t)

	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	me := root.get("/api/auth/me")
	expect(t, "root me", me, http.StatusOK)
	id := me.str("accountId")
	if id == "" {
		t.Fatalf("平台管理员应有 accountId: %s", me.raw)
	}

	expect(t, "self demote", root.post("/admin/accounts/"+id, `{"platform_admin":false}`), http.StatusConflict)
	expect(t, "self disable", root.post("/admin/accounts/"+id, `{"disabled":true}`), http.StatusConflict)

	after := root.get("/api/auth/me")
	expect(t, "me after rejected writes", after, http.StatusOK)
	if admin, _ := after.obj()["platformAdmin"].(bool); !admin || after.str("username") != e2eSeedUser {
		t.Fatalf("拒绝后平台管理员身份不应变化: %s", after.raw)
	}

	// 另一个人（非自己）仍可被降级/禁用。
	other := e.adminClient().post("/admin/accounts", `{"username":"frank","password":"pass-frank-12345","platform_admin":true}`)
	expect(t, "create admin frank", other, http.StatusOK)
	expect(t, "demote other admin", root.post("/admin/accounts/"+other.str("id"), `{"platform_admin":false}`), http.StatusOK)
}
