package app

import (
	"net/http"
	"strings"
	"testing"
)

// TestE2EInviteFlow 覆盖创建 → 未登录跳转 → GET 不消费 → POST 兑换 → 二次失败。
func TestE2EInviteFlow(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	carol := e.loginAs("carol") // cube-a owner

	created := carol.post("/admin/tenants/cube-a/invites", `{"role":"editor"}`)
	expect(t, "create invite", created, http.StatusOK)
	path := created.str("path")
	if !strings.HasPrefix(path, "/__invite/inv_") || created.str("role") != "editor" {
		t.Fatalf("邀请响应不对: %s", created.raw)
	}

	list := carol.get("/admin/tenants/cube-a/invites")
	expect(t, "list invites", list, http.StatusOK)
	records := list.records()
	if len(records) != 1 || records[0]["status"] != "active" || strings.Contains(list.raw, "inv_") {
		t.Fatalf("列表应只含状态且不回显 token: %s", list.raw)
	}

	// 未登录 GET 跳登录页，且带回跳。
	anon := e.client()
	loginJump := anon.get(path)
	expect(t, "anon get invite", loginJump, http.StatusSeeOther)
	if loc := loginJump.header.Get("Location"); !strings.HasPrefix(loc, "/__login?next=") {
		t.Fatalf("未登录应跳到登录页: %q", loc)
	}

	dave := e.loginAs("dave")
	confirm := dave.get(path)
	expect(t, "dave get invite", confirm, http.StatusOK)
	if !strings.Contains(confirm.raw, "加入项目") || !strings.Contains(confirm.raw, "编辑者") {
		t.Fatalf("确认页缺少项目/角色: %s", confirm.raw)
	}
	// GET 两次都不消费。
	expect(t, "second get invite", dave.get(path), http.StatusOK)
	still := carol.get("/admin/tenants/cube-a/invites")
	if got := still.records(); len(got) != 1 || got[0]["status"] != "active" {
		t.Fatalf("GET 不应消费邀请: %s", still.raw)
	}

	expect(t, "dave redeems", dave.post(path, ""), http.StatusSeeOther)
	dict := dave.get("/api/dashboard/project/dict")
	if !hasProject(dict.list(), "cube-a") || enabledProject(dict.list()) != "cube-a" {
		t.Fatalf("兑换后应加入 cube-a 并切为当前项目: %s", dict.raw)
	}
	used := carol.get("/admin/tenants/cube-a/invites")
	if got := used.records(); len(got) != 1 || got[0]["status"] != "used" || got[0]["usedByAccountId"] == "" {
		t.Fatalf("兑换后状态应为 used: %s", used.raw)
	}

	// 本人重复点击是幂等的；其他账号再用必须失败。
	expect(t, "dave redeems again", dave.post(path, ""), http.StatusSeeOther)
	expect(t, "alice cannot reuse", e.loginAs("alice").post(path, ""), http.StatusNotFound)
	expect(t, "revoke used invite", carol.post("/admin/tenants/cube-a/invites/"+created.str("id")+"/revoke", ""), http.StatusConflict)

	// 成员关系确实生效：dave 可以写 cube-a（editor）。
	expect(t, "dave writes as editor", dave.post("/api/dashboard/keys/create", `{"projectId":"cube-a","keyName":"from-invite"}`), http.StatusOK)
}

func TestE2EInvitePermissions(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)

	// owner 不能创建 owner 邀请；平台管理员可以。
	carol := e.loginAs("carol")
	expect(t, "owner role invite by owner", carol.post("/admin/tenants/cube-a/invites", `{"role":"owner"}`), http.StatusForbidden)
	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	expect(t, "owner role invite by platform admin", root.post("/admin/tenants/cube-a/invites", `{"role":"owner"}`), http.StatusOK)

	// viewer 不能创建、不能列出。
	alice := e.loginAs("alice")
	expect(t, "viewer create invite", alice.post("/admin/tenants/cube-a/invites", `{"role":"viewer"}`), http.StatusForbidden)
	expect(t, "viewer list invites", alice.get("/admin/tenants/cube-a/invites"), http.StatusForbidden)

	// 角色非法、过期时间越界都是 400。
	expect(t, "invalid role", carol.post("/admin/tenants/cube-a/invites", `{"role":"admin"}`), http.StatusBadRequest)
	expect(t, "too long expiry", carol.post("/admin/tenants/cube-a/invites", `{"role":"viewer","expiresIn":"900h"}`), http.StatusBadRequest)
	expect(t, "bad duration", carol.post("/admin/tenants/cube-a/invites", `{"role":"viewer","expiresIn":"soon"}`), http.StatusBadRequest)

	// 管理 Token 不能兑换邀请。
	created := carol.post("/admin/tenants/cube-a/invites", `{"role":"viewer"}`)
	expect(t, "create for token redeem", created, http.StatusOK)
	expect(t, "admin token redeems", e.adminClient().post(created.str("path"), ""), http.StatusForbidden)

	// 成员候选：只有 owner 能搜，空查询返回空。
	expect(t, "viewer candidates", alice.get("/admin/tenants/cube-a/member-candidates?query=da"), http.StatusForbidden)
	candidates := carol.get("/admin/tenants/cube-a/member-candidates?query=@dav")
	expect(t, "owner candidates", candidates, http.StatusOK)
	records := candidates.records()
	if len(records) != 1 {
		t.Fatalf("候选应有 1 条: %s", candidates.raw)
	}
	member, isBool := records[0]["member"].(bool)
	if records[0]["username"] != "dave" || !isBool || member {
		t.Fatalf("候选应找到 dave 且标注未加入: %s", candidates.raw)
	}
	empty := carol.get("/admin/tenants/cube-a/member-candidates?query=")
	expect(t, "empty candidates", empty, http.StatusOK)
	if len(empty.records()) != 0 {
		t.Fatalf("空查询应返回空列表: %s", empty.raw)
	}
	// 单字符查询只做用户名精确匹配：合法的 1 字符用户名仍能被找到。
	admin := e.adminClient()
	expect(t, "create one-char account", admin.post("/admin/accounts", `{"username":"q","password":"pass-q-12345"}`), http.StatusOK)
	exact := carol.get("/admin/tenants/cube-a/member-candidates?query=q")
	expect(t, "one-char exact candidates", exact, http.StatusOK)
	if got := exact.records(); len(got) != 1 || got[0]["username"] != "q" {
		t.Fatalf("单字符查询应只精确命中 q: %s", exact.raw)
	}
}

// TestE2EInviteReplayAfterRemoval 证明“本人重复点击”不会重建已被移除的成员关系。
func TestE2EInviteReplayAfterRemoval(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	carol := e.loginAs("carol")
	created := carol.post("/admin/tenants/cube-a/invites", `{"role":"editor"}`)
	expect(t, "create invite", created, http.StatusOK)
	path := created.str("path")

	dave := e.loginAs("dave")
	expect(t, "dave redeems", dave.post(path, ""), http.StatusSeeOther)
	expect(t, "carol removes dave", carol.post("/admin/tenants/cube-a/members/"+ids["dave"]+"/remove", ""), http.StatusOK)

	// 旧链接不能把人加回来，也不能把当前项目切回 cube-a。
	expect(t, "dave replays after removal", dave.post(path, ""), http.StatusNotFound)
	expect(t, "dave previews after removal", dave.get(path), http.StatusNotFound)
	dict := dave.get("/api/dashboard/project/dict")
	if hasProject(dict.list(), "cube-a") {
		t.Fatalf("被移出后不应仍在 cube-a: %s", dict.raw)
	}
}

// TestE2EInviteRejectsCrossSitePost 证明浏览器跨站表单不能兑换邀请。
func TestE2EInviteRejectsCrossSitePost(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	carol := e.loginAs("carol")
	created := carol.post("/admin/tenants/cube-a/invites", `{"role":"viewer"}`)
	expect(t, "create invite", created, http.StatusOK)
	path := created.str("path")

	dave := e.loginAs("dave")
	dave.headers = map[string]string{"Origin": "https://evil.example"}
	expect(t, "cross-site origin", dave.post(path, ""), http.StatusForbidden)
	dave.headers = map[string]string{"Sec-Fetch-Site": "cross-site"}
	expect(t, "cross-site fetch metadata", dave.post(path, ""), http.StatusForbidden)
	// 网关改写了 Host 时，Origin 与 X-Forwarded-Host 一致应放行（列表取第一段）。
	dave.headers = map[string]string{"Origin": "https://memos.example", "X-Forwarded-Host": "memos.example, gw"}
	expect(t, "gateway rewritten host redeems", dave.post(path, ""), http.StatusSeeOther)
}
