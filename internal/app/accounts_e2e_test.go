package app

import (
	"fmt"
	"net/http"
	"strings"
	"testing"
)

// seedWorld 用管理 Token 建好四个账号与成员关系，返回各账号 ID。
//
//	alice: cube-a viewer    bob: cube-a editor    carol: cube-a/cube-b owner    dave: 无项目
func seedWorld(t *testing.T, e *e2eEnv) map[string]string {
	t.Helper()
	admin := e.adminClient()
	ids := map[string]string{}
	for _, name := range []string{"alice", "bob", "carol", "dave"} {
		created := admin.post("/admin/accounts", fmt.Sprintf(`{"username":%q,"password":"pass-%s-12345","display_name":%q}`, name, name, name))
		expect(t, "create "+name, created, http.StatusOK)
		ids[name] = created.str("id")
	}
	members := []struct{ tenant, user, role string }{
		{"cube-a", "alice", "viewer"},
		{"cube-a", "bob", "editor"},
		{"cube-a", "carol", "owner"},
		{"cube-b", "carol", "owner"},
	}
	for _, m := range members {
		got := admin.post("/admin/tenants/"+m.tenant+"/members", fmt.Sprintf(`{"username":%q,"role":%q}`, m.user, m.role))
		expect(t, "member "+m.user, got, http.StatusOK)
	}
	return ids
}

func (e *e2eEnv) loginAs(name string) *e2eClient {
	e.t.Helper()
	c := e.client()
	expect(e.t, "login "+name, c.login(name, "pass-"+name+"-12345"), http.StatusOK)
	return c
}

func TestE2EUnauthenticated(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	anon := e.client()

	for _, path := range []string{"/api/dashboard/project/dict", "/admin/tenants", "/api/auth/me"} {
		got := anon.get(path)
		expect(t, path, got, http.StatusUnauthorized)
		if got.code != 40100 || got.msg != "登录已过期，请重新登录" {
			t.Fatalf("%s: envelope code=%d msg=%q", path, got.code, got.msg)
		}
	}

	meta := anon.get("/api/meta")
	expect(t, "meta", meta, http.StatusOK)
	if enabled, _ := meta.obj()["authEnabled"].(bool); !enabled || meta.str("authMode") != "page" {
		t.Fatalf("meta 缺少 auth 字段: %s", meta.raw)
	}
	if _, leaked := meta.obj()["sources"]; leaked {
		t.Fatalf("未登录的 meta 不应带 sources: %s", meta.raw)
	}

	root := e.client()
	expect(t, "seed login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	logged := root.get("/api/meta")
	if _, ok := logged.obj()["sources"]; !ok {
		t.Fatalf("登录后的 meta 应带 sources: %s", logged.raw)
	}
}

func TestE2ELoginErrorsAreIndistinguishable(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	expect(t, "disable dave", e.adminClient().post("/admin/accounts/"+ids["dave"], `{"disabled":true}`), http.StatusOK)

	wrong := e.client().login("alice", "not-the-password")
	missing := e.client().login("nobody", "whatever-123")
	disabled := e.client().login("dave", "pass-dave-12345")
	for label, got := range map[string]result{"wrong": wrong, "missing": missing, "disabled": disabled} {
		expect(t, label, got, http.StatusUnauthorized)
		if got.msg != wrong.msg || got.code != wrong.code {
			t.Fatalf("%s 的错误与口令错误可区分: %q vs %q", label, got.msg, wrong.msg)
		}
	}
}

func TestE2ELoginThrottle(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	c := e.client()
	for range 5 {
		expect(t, "bad login", c.login("alice", "wrong-password"), http.StatusUnauthorized)
	}
	got := c.login("alice", "wrong-password")
	expect(t, "throttled", got, http.StatusTooManyRequests)
	if got.code != 42900 || got.msg != "尝试次数过多，请稍后再试" {
		t.Fatalf("限流信封不符: %s", got.raw)
	}
	// 同一来源 IP 超限后，换用户名也被拦下，连正确口令都不再尝试。
	expect(t, "ip throttled", c.login(e2eSeedUser, e2eSeedPass), http.StatusTooManyRequests)
}

func TestE2ELoginPageAndCookie(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	c := e.client()

	page := c.do(http.MethodGet, "/__login", "", "")
	expect(t, "login page", page, http.StatusOK)
	if !strings.Contains(page.raw, `name="username"`) || strings.Contains(page.raw, "http://") || strings.Contains(page.raw, "https://") {
		t.Fatalf("登录页缺字段或引用了外部资源")
	}

	form := "username=" + e2eSeedUser + "&password=bad-password-zzz"
	bad := c.do(http.MethodPost, "/__login", "application/x-www-form-urlencoded", form)
	expect(t, "form bad login", bad, http.StatusUnauthorized)
	if strings.Contains(bad.raw, "bad-password-zzz") || !strings.Contains(bad.raw, "用户名或口令错误") {
		t.Fatalf("失败页应只含固定文案、不回显输入")
	}

	req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, e.srv.URL+"/__login",
		strings.NewReader("username="+e2eSeedUser+"&password="+e2eSeedPass))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.http.Do(req)
	if err != nil {
		t.Fatalf("form login: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusSeeOther || resp.Header.Get("Location") != "/" {
		t.Fatalf("form login = %d Location=%q", resp.StatusCode, resp.Header.Get("Location"))
	}
	cookie := resp.Header.Get("Set-Cookie")
	for _, want := range []string{"memos_console_sid=", "HttpOnly", "SameSite=Strict", "Path=/"} {
		if !strings.Contains(cookie, want) {
			t.Fatalf("cookie 缺少 %q: %s", want, cookie)
		}
	}
	expect(t, "me after form login", c.get("/api/auth/me"), http.StatusOK)
}

func TestE2EAccountListQueryAndDetail(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	admin := e.adminClient()

	list := admin.get("/admin/accounts?query=ali&page=1&pageSize=10")
	expect(t, "list query", list, http.StatusOK)
	if total, _ := list.obj()["total"].(float64); total != 1 {
		t.Fatalf("过滤后总数 = %v, want 1: %s", total, list.raw)
	}
	if recs := list.records(); len(recs) != 1 || recs[0]["username"] != "alice" {
		t.Fatalf("过滤结果 = %v", recs)
	}

	detail := admin.get("/admin/accounts/" + ids["bob"])
	expect(t, "account detail", detail, http.StatusOK)
	if detail.str("username") != "bob" {
		t.Fatalf("detail = %s", detail.raw)
	}
	expect(t, "missing account", admin.get("/admin/accounts/nope"), http.StatusNotFound)
}

func TestE2ESelfServicePasswordAndSessions(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	first := e.loginAs("alice")
	second := e.loginAs("alice")

	sessions := first.get("/api/auth/sessions")
	expect(t, "sessions", sessions, http.StatusOK)
	rows, _ := sessions.obj()["sessions"].([]any)
	current := 0
	for _, row := range rows {
		if m, ok := row.(map[string]any); ok {
			if cur, _ := m["current"].(bool); cur {
				current++
			}
		}
	}
	if len(rows) != 2 || current != 1 {
		t.Fatalf("会话列表 = %s", sessions.raw)
	}

	wrong := first.post("/api/auth/password", `{"oldPassword":"wrong-pass","newPassword":"newpass-12345"}`)
	expect(t, "wrong old password", wrong, http.StatusUnauthorized)

	changed := first.post("/api/auth/password", `{"oldPassword":"pass-alice-12345","newPassword":"newpass-12345"}`)
	expect(t, "change password", changed, http.StatusOK)

	expect(t, "current session survives", first.get("/api/auth/me"), http.StatusOK)
	expect(t, "other session revoked", second.get("/api/auth/me"), http.StatusUnauthorized)
	expect(t, "old password rejected", e.client().login("alice", "pass-alice-12345"), http.StatusUnauthorized)
	expect(t, "new password accepted", e.client().login("alice", "newpass-12345"), http.StatusOK)
}

func TestE2ERevokeOtherSessions(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	keep := e.loginAs("alice")
	other := e.loginAs("alice")

	revoked := keep.post("/api/auth/sessions/revoke", "")
	expect(t, "revoke others", revoked, http.StatusOK)
	if n, _ := revoked.obj()["revoked"].(float64); n < 1 {
		t.Fatalf("revoked = %v", revoked.obj()["revoked"])
	}
	expect(t, "kept session", keep.get("/api/auth/me"), http.StatusOK)
	expect(t, "revoked session", other.get("/api/auth/me"), http.StatusUnauthorized)
}

func TestE2ELastProjectOwnerIsProtected(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	admin := e.adminClient()

	demote := admin.post("/admin/tenants/cube-a/members", `{"username":"carol","role":"editor"}`)
	expect(t, "demote last owner", demote, http.StatusConflict)
	remove := admin.post("/admin/tenants/cube-a/members/"+ids["carol"]+"/remove", "")
	expect(t, "remove last owner", remove, http.StatusConflict)

	expect(t, "add second owner", admin.post("/admin/tenants/cube-a/members", `{"username":"alice","role":"owner"}`), http.StatusOK)
	expect(t, "demote after second owner", admin.post("/admin/tenants/cube-a/members", `{"username":"carol","role":"editor"}`), http.StatusOK)
}
