package app

import (
	"net/http"
	"net/url"
	"strconv"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// TestE2ELoginThrottleRetryAfter 验证登录节流触发 429 时带窗口上限（秒）。
func TestE2ELoginThrottleRetryAfter(t *testing.T) {
	t.Parallel()
	e := newE2EWithAuth(t, config.AuthConfig{
		SeedUser:           e2eSeedUser,
		SeedPassword:       e2eSeedPass,
		SessionTTL:         12 * time.Hour,
		SessionMaxLifetime: 30 * 24 * time.Hour,
		AllowPassword:      true,
		MaxLoginFailures:   2,
		LoginFailWindow:    90 * time.Second,
	})
	c := e.client()
	var out result
	for range 5 {
		out = c.login(e2eSeedUser, "wrong-pass-1")
		if out.status == http.StatusTooManyRequests {
			break
		}
	}
	if out.status != http.StatusTooManyRequests {
		t.Fatalf("throttled status=%d want 429 body=%s", out.status, out.raw)
	}
	got := out.header.Get("Retry-After")
	seconds, err := strconv.Atoi(got)
	if err != nil || seconds <= 0 {
		t.Fatalf("Retry-After = %q, want positive integer", got)
	}
	if seconds != 90 {
		t.Fatalf("Retry-After = %d, want 90 (窗口上限)", seconds)
	}
}

func TestE2EDisableAndLogout(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	admin := e.adminClient()

	alice := e.loginAs("alice")
	expect(t, "alice before", alice.get("/api/auth/me"), http.StatusOK)
	expect(t, "disable alice", admin.post("/admin/accounts/"+ids["alice"], `{"disabled":true}`), http.StatusOK)
	expect(t, "old session after disable", alice.get("/api/auth/me"), http.StatusUnauthorized)
	expect(t, "login while disabled", e.client().login("alice", "pass-alice-12345"), http.StatusUnauthorized)
	expect(t, "enable alice", admin.post("/admin/accounts/"+ids["alice"], `{"disabled":false}`), http.StatusOK)
	expect(t, "old session stays revoked", alice.get("/api/auth/me"), http.StatusUnauthorized)
	expect(t, "login after enable", e.client().login("alice", "pass-alice-12345"), http.StatusOK)

	bob := e.loginAs("bob")
	out := bob.do(http.MethodGet, "/__logout", "", "")
	expect(t, "logout", out, http.StatusSeeOther)
	expect(t, "cookie after logout", bob.get("/api/auth/me"), http.StatusUnauthorized)
	expect(t, "logout without session", e.client().do(http.MethodGet, "/__logout", "", ""), http.StatusSeeOther)
}

func TestE2EPasswordReset(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)

	bob := e.loginAs("bob")
	expect(t, "short password rejected", e.adminClient().post("/admin/accounts/"+ids["bob"]+"/password", `{"password":"short"}`), http.StatusBadRequest)
	expect(t, "reset", e.adminClient().post("/admin/accounts/"+ids["bob"]+"/password", `{"password":"brand-new-pass-1"}`), http.StatusOK)
	expect(t, "old session revoked", bob.get("/api/auth/me"), http.StatusUnauthorized)
	expect(t, "old password", e.client().login("bob", "pass-bob-12345"), http.StatusUnauthorized)
	expect(t, "new password", e.client().login("bob", "brand-new-pass-1"), http.StatusOK)

	// 被锁定后，管理员重置口令应同时清掉失败计数，新口令立即可用。
	// IP 维度可能已有前面用例留下的失败计数，所以循环到 429 为止。
	for range 10 {
		out := e.client().login("bob", "wrong-pass-1")
		if out.status == http.StatusTooManyRequests {
			break
		}
		expect(t, "wrong password", out, http.StatusUnauthorized)
	}
	expect(t, "throttled", e.client().login("bob", "brand-new-pass-1"), http.StatusTooManyRequests)
	expect(t, "unlock by reset", e.adminClient().post("/admin/accounts/"+ids["bob"]+"/password", `{"password":"unlocked-pass-1"}`), http.StatusOK)
	expect(t, "login after unlock", e.client().login("bob", "unlocked-pass-1"), http.StatusOK)

	carol := e.loginAs("carol")
	expect(t, "revoke sessions", e.adminClient().post("/admin/accounts/"+ids["carol"]+"/sessions/revoke", ""), http.StatusOK)
	expect(t, "carol revoked", carol.get("/api/auth/me"), http.StatusUnauthorized)
}

func TestE2EKeepOnePlatformAdmin(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	admin := e.adminClient()

	list := admin.get("/admin/accounts")
	expect(t, "list accounts", list, http.StatusOK)
	recs := list.records()
	if admin, _ := recs[0]["platform_admin"].(bool); len(recs) != 1 || recs[0]["username"] != e2eSeedUser || !admin {
		t.Fatalf("应只有种子管理员: %s", list.raw)
	}
	if _, leaked := recs[0]["password_hash"]; leaked {
		t.Fatal("账号列表泄漏口令哈希")
	}
	rootID, _ := recs[0]["id"].(string)

	expect(t, "disable last admin", admin.post("/admin/accounts/"+rootID, `{"disabled":true}`), http.StatusConflict)
	expect(t, "demote last admin", admin.post("/admin/accounts/"+rootID, `{"platform_admin":false}`), http.StatusConflict)

	second := admin.post("/admin/accounts", `{"username":"ops","password":"ops-pass-12345","platform_admin":true}`)
	expect(t, "second admin", second, http.StatusOK)
	expect(t, "disable first admin", admin.post("/admin/accounts/"+rootID, `{"disabled":true}`), http.StatusOK)
	expect(t, "disable last remaining", admin.post("/admin/accounts/"+second.str("id"), `{"disabled":true}`), http.StatusConflict)
	expect(t, "admin token is the break-glass channel", admin.get("/admin/tenants"), http.StatusOK)
	expect(t, "duplicate username", admin.post("/admin/accounts", `{"username":"OPS ","password":"ops-pass-12345"}`), http.StatusConflict)
	expect(t, "bad username", admin.post("/admin/accounts", `{"username":"has space","password":"ops-pass-12345"}`), http.StatusBadRequest)
}

func TestE2ESeedAdminIsNotOverwritten(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	// 种子管理员已存在时重启不覆盖口令：改密后用旧种子口令不能再登录。
	list := e.adminClient().get("/admin/accounts")
	var rootID string
	for _, rec := range list.records() {
		if rec["username"] == e2eSeedUser {
			rootID, _ = rec["id"].(string)
		}
	}
	expect(t, "reset root", e.adminClient().post("/admin/accounts/"+rootID+"/password", `{"password":"changed-root-pass-1"}`), http.StatusOK)

	if err := e.app.accounts.Bootstrap(t.Context()); err != nil {
		t.Fatalf("bootstrap again: %v", err)
	}
	expect(t, "seed does not overwrite", e.client().login(e2eSeedUser, e2eSeedPass), http.StatusUnauthorized)
	expect(t, "changed password works", e.client().login(e2eSeedUser, "changed-root-pass-1"), http.StatusOK)
}

func TestE2EAuditCoversOnlyLivePrincipals(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)

	anon := e.client()
	expect(t, "anonymous meta", anon.get("/api/meta"), http.StatusOK)
	expect(t, "anonymous dict", anon.get("/api/dashboard/project/dict"), http.StatusUnauthorized)
	expect(t, "anonymous admin", anon.get("/admin/tenants"), http.StatusUnauthorized)

	alice := e.loginAs("alice")
	base, err := url.Parse(e.srv.URL)
	if err != nil {
		t.Fatalf("parse url: %v", err)
	}
	var sid string
	for _, c := range alice.http.Jar.Cookies(base) {
		if c.Name == httpx.SessionCookie {
			sid = c.Value
		}
	}
	if sid == "" {
		t.Fatal("登录后 jar 里应有会话 cookie")
	}
	expect(t, "alice dict", alice.get("/api/dashboard/project/dict"), http.StatusOK)
	expect(t, "alice logout", alice.do(http.MethodGet, "/__logout", "", ""), http.StatusSeeOther)

	for label, value := range map[string]string{"replayed": sid, "forged": "forged-session-value"} {
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, e.srv.URL+"/__logout", nil)
		if err != nil {
			t.Fatalf("new request: %v", err)
		}
		req.AddCookie(&http.Cookie{Name: httpx.SessionCookie, Value: value})
		resp, err := anon.http.Do(req)
		if err != nil {
			t.Fatalf("%s logout: %v", label, err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusSeeOther {
			t.Fatalf("%s logout = %d", label, resp.StatusCode)
		}
	}

	if err := e.app.audit.Stop(t.Context()); err != nil {
		t.Fatalf("drain audit: %v", err)
	}
	records, _, err := e.app.store.ListAudit(t.Context(), domain.AuditFilter{Limit: 500})
	if err != nil {
		t.Fatalf("list audit: %v", err)
	}
	logouts := 0
	for i := range records {
		if records[i].UserID == "" {
			t.Fatalf("未认证请求不应入审计: tool=%s", records[i].Tool)
		}
		if records[i].Tool == "auth.logout" {
			logouts++
			if records[i].UserID != "alice" {
				t.Fatalf("auth.logout 应带用户名 alice: %q", records[i].UserID)
			}
		}
	}
	if logouts != 1 {
		t.Fatalf("只有真实吊销有效会话的那次登出应入审计，实际 %d 条", logouts)
	}
}
