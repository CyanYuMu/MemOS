package app

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const e2eZTSecret = "e2e-zerotrust-secret-0123456789"

func ztE2EToken(t *testing.T, username string, exp time.Time) string {
	t.Helper()
	enc := base64.RawURLEncoding
	head, err := json.Marshal(map[string]string{"alg": "HS256", "typ": "JWT"})
	if err != nil {
		t.Fatalf("marshal header: %v", err)
	}
	body, err := json.Marshal(map[string]any{
		"iat": time.Now().Unix(), "exp": exp.Unix(), "username": username, "name": "测试用户",
		"email": username + "@example.com",
	})
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	signing := enc.EncodeToString(head) + "." + enc.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(e2eZTSecret))
	mac.Write([]byte(signing))
	return signing + "." + enc.EncodeToString(mac.Sum(nil))
}

func newE2EZeroTrust(t *testing.T, autoCreate bool, allowed []string) *e2eEnv {
	t.Helper()
	return newE2EWithAuth(t, config.AuthConfig{
		SeedUser:           e2eSeedUser,
		SeedPassword:       e2eSeedPass,
		SessionTTL:         12 * time.Hour,
		SessionMaxLifetime: 30 * 24 * time.Hour,
		AllowPassword:      false,
		ZeroTrust: config.ZeroTrustConfig{
			Enabled:      true,
			Required:     true,
			AutoCreate:   autoCreate,
			Secrets:      []string{e2eZTSecret},
			AllowedUsers: allowed,
			LogoutURL:    "https://example.com/logout",
		},
	})
}

func (e *e2eEnv) ztClient(username string) *e2eClient {
	c := e.client()
	c.headers = map[string]string{"X-Zt-Authorization": ztE2EToken(e.t, username, time.Now().Add(5*time.Minute))}
	return c
}

func TestE2EZeroTrustAutoCreatesAccount(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, true, nil)

	anon := e.client()
	expect(t, "no header", anon.get("/api/auth/me"), http.StatusUnauthorized)
	expect(t, "password login disabled", anon.login(e2eSeedUser, e2eSeedPass), http.StatusForbidden)

	// 前端据此区分身份来源。
	meta := anon.get("/api/meta")
	expect(t, "meta", meta, http.StatusOK)
	if mode := meta.obj()["authMode"]; mode != "external" {
		t.Fatalf("authMode = %v，期望 external", mode)
	}

	client := e.ztClient("alice.zhang")
	me := client.get("/api/auth/me")
	expect(t, "zt me", me, http.StatusOK)
	body := me.obj()
	if body["username"] != "alice.zhang" || body["displayName"] != "测试用户" {
		t.Fatalf("me = %s", me.raw)
	}
	if admin, _ := body["platformAdmin"].(bool); admin {
		t.Fatalf("自动建号不应是平台管理员: %s", me.raw)
	}
	// 第二次请求命中同一账号，不会重复建号。
	accounts := e.adminClient().get("/admin/accounts")
	expect(t, "list accounts", accounts, http.StatusOK)
	if n := len(accounts.records()); n != 2 {
		t.Fatalf("账号数 = %d（种子 + 自动建号），raw=%s", n, accounts.raw)
	}
	for _, row := range accounts.records() {
		if row["username"] != "alice.zhang" {
			continue
		}
		if row["email"] != "alice.zhang@example.com" || row["display_name"] != "测试用户" {
			t.Fatalf("账号资料未落库: %v", row)
		}
	}
	expect(t, "zt me again", client.get("/api/auth/me"), http.StatusOK)
}

// TestE2EZeroTrustManualUploadUsesAccountIdentity 零信任外部身份没有 SessionID，
// 手工上传不能因此被当成“未登录”（400）。没有项目成员关系时应得到 403。
func TestE2EZeroTrustManualUploadUsesAccountIdentity(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, true, nil)

	client := e.ztClient("carol.wu")
	res := client.post("/api/local-sync/manual",
		`{"projectId":"cube-none","batchId":"batch-manual-1","layers":["policy"],"manifest":{"policy":0},"traceMode":"summary","rows":[]}`)
	if res.status != http.StatusForbidden {
		t.Fatalf("manual as external identity = %d %s，期望 403（没有项目权限），不应是 400",
			res.status, res.raw)
	}
}

func TestE2EZeroTrustRejections(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, true, []string{"allowed.user"})

	expect(t, "not in allowlist", e.ztClient("other.user").get("/api/auth/me"), http.StatusForbidden)

	bad := e.client()
	bad.headers = map[string]string{"X-Zt-Authorization": "not-a-jwt"}
	expect(t, "invalid token", bad.get("/api/auth/me"), http.StatusUnauthorized)

	expired := e.client()
	expired.headers = map[string]string{"X-Zt-Authorization": ztE2EToken(t, "allowed.user", time.Now().Add(-time.Hour))}
	expect(t, "expired token", expired.get("/api/auth/me"), http.StatusUnauthorized)

	// 白名单用户第一次出现即建号，随后被管理员禁用则不能再进。
	expect(t, "allowlisted", e.ztClient("allowed.user").get("/api/auth/me"), http.StatusOK)
	ids := e.adminClient().get("/admin/accounts")
	expect(t, "list accounts", ids, http.StatusOK)
	var id string
	for _, row := range ids.records() {
		if row["username"] == "allowed.user" {
			id, _ = row["id"].(string)
		}
	}
	if id == "" {
		t.Fatalf("没有找到自动建号: %s", ids.raw)
	}
	expect(t, "disable", e.adminClient().post("/admin/accounts/"+id, `{"disabled":true}`), http.StatusOK)
	expect(t, "disabled account", e.ztClient("allowed.user").get("/api/auth/me"), http.StatusForbidden)
}

// TestE2EZeroTrustRejectsSessionCookie Required 模式下本地会话不再单独通过，
// 即使 cookie 是真实有效的。
func TestE2EZeroTrustRejectsSessionCookie(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, true, nil)

	ctx := context.Background()
	acct, err := e.app.store.AccountByUsername(ctx, e2eSeedUser)
	if err != nil {
		t.Fatalf("seed account: %v", err)
	}
	raw := "e2e-session-cookie-000000000001"
	if err := e.app.store.CreateSession(ctx, domain.Session{
		ID:        domain.NewID(),
		Hash:      domain.HashToken(raw),
		AccountID: acct.ID,
		CreatedAt: time.Now(),
		ExpiresAt: time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("create session: %v", err)
	}

	client := e.client()
	client.headers = map[string]string{"Cookie": "memos_console_sid=" + raw}
	expect(t, "session cookie ignored", client.get("/api/auth/me"), http.StatusUnauthorized)
}

func TestE2EZeroTrustNoAutoCreate(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, false, nil)

	expect(t, "unknown user", e.ztClient("nobody").get("/api/auth/me"), http.StatusForbidden)
	accounts := e.adminClient().get("/admin/accounts")
	expect(t, "list accounts", accounts, http.StatusOK)
	if n := len(accounts.records()); n != 1 {
		t.Fatalf("账号数 = %d，应只有种子账号", n)
	}
}

// TestE2EZeroTrustSwitchProject 外部身份没有会话，切换项目要落在账号级偏好上。
func TestE2EZeroTrustSwitchProject(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, true, nil)

	user := e.ztClient("multi.project")
	expect(t, "zt me", user.get("/api/auth/me"), http.StatusOK)

	ids := e.adminClient().get("/admin/accounts")
	expect(t, "list accounts", ids, http.StatusOK)
	var id string
	for _, row := range ids.records() {
		if row["username"] == "multi.project" {
			id, _ = row["id"].(string)
		}
	}
	if id == "" {
		t.Fatalf("没有找到自动建号: %s", ids.raw)
	}
	for _, tenant := range []string{"cube-a", "cube-b"} {
		body := `{"account_id":"` + id + `","role":"viewer"}`
		expect(t, "join "+tenant, e.adminClient().post("/admin/tenants/"+tenant+"/members", body), http.StatusOK)
	}

	me := user.get("/api/auth/me")
	expect(t, "me after join", me, http.StatusOK)
	if got := me.str("currentProjectId"); got != "u-"+id {
		t.Fatalf("currentProjectId = %q，未设置时默认应指向自动建号的个人项目 u-%s", got, id)
	}

	expect(t, "switch to cube-b", user.post("/api/dashboard/project/update", `{"id":"cube-b","current":true}`), http.StatusOK)
	me = user.get("/api/auth/me")
	expect(t, "me after switch", me, http.StatusOK)
	if got := me.str("currentProjectId"); got != "cube-b" {
		t.Fatalf("currentProjectId = %q，切换后应为 cube-b", got)
	}
}

func TestE2EZeroTrustLogout(t *testing.T) {
	t.Parallel()
	e := newE2EZeroTrust(t, true, nil)

	user := e.ztClient("logout.user")
	expect(t, "zt me", user.get("/api/auth/me"), http.StatusOK)

	out := user.get("/__logout")
	expect(t, "logout", out, http.StatusSeeOther)
	if got := out.header.Get("Location"); got != "https://example.com/logout" {
		t.Fatalf("Location = %q", got)
	}
	cookies := strings.Join(out.header.Values("Set-Cookie"), "\n")
	if !strings.Contains(cookies, "SECURE_ZT_GW_TOKEN=") || !strings.Contains(cookies, "Max-Age=0") {
		t.Fatalf("登出应清掉网关 cookie，实际 Set-Cookie=%q", cookies)
	}
	if !strings.Contains(cookies, httpx.LoggedOutCookie+"=1") {
		t.Fatalf("登出应写入退出标记，实际 Set-Cookie=%q", cookies)
	}

	// 网关会在每个请求上注入身份；没有退出标记时用户会被静默登回来。这里验证
	// 同一浏览器在 /__relogin 之前始终停在退出态。
	user.headers["Accept"] = "text/html"
	page := user.get("/")
	delete(user.headers, "Accept")
	expect(t, "logged out page", page, http.StatusOK)
	if !strings.Contains(page.raw, "已退出登录") {
		t.Fatalf("退出后应看到退出页: %s", page.raw)
	}
	expect(t, "api after logout", user.get("/api/auth/me"), http.StatusUnauthorized)

	// 重新登录是显式动作：清标记后身份仍由网关决定。
	expect(t, "relogin", user.get("/__relogin"), http.StatusSeeOther)
	expect(t, "api after relogin", user.get("/api/auth/me"), http.StatusOK)
}
