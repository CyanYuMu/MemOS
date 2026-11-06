package auth

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func sidCookie(v string) reqOpt { return header("Cookie", httpx.SessionCookie+"="+v) }

func TestLogout(t *testing.T) {
	t.Parallel()
	t.Run("清会话 cookie、立起退出标记并回首页", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.logoutName = "alice"
		rec := h.do(t, http.MethodPost, "/__logout", "", sidCookie("abc"))

		if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/" {
			t.Fatalf("status=%d location=%q", rec.Code, rec.Header().Get("Location"))
		}
		if h.sessions.logoutRaw != "abc" {
			t.Fatalf("Logout 收到 %q", h.sessions.logoutRaw)
		}
		if c := cookieNamed(rec, httpx.SessionCookie); c == nil || c.MaxAge >= 0 || c.Value != "" {
			t.Fatalf("会话 cookie 应被清除: %+v", c)
		}
		if c := cookieNamed(rec, httpx.LoggedOutCookie); c == nil || c.Value != "1" || !c.HttpOnly || c.MaxAge != 0 {
			t.Fatalf("退出标记应是会话级 cookie: %+v", c)
		}
		if cookieNamed(rec, gatewayCookie) != nil {
			t.Fatal("没有配置退出地址时不动网关 cookie")
		}
	})

	t.Run("配置了统一认证退出地址就跳过去并清网关 cookie", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t, func(d *Deps) { d.LogoutURL = "https://uuap.example.com/logout" })
		rec := h.do(t, http.MethodGet, "/__logout", "", func(r *http.Request) { r.Host = "console.example.com:5280" })
		if rec.Header().Get("Location") != "https://uuap.example.com/logout" {
			t.Fatalf("location = %q", rec.Header().Get("Location"))
		}
		c := cookieNamed(rec, gatewayCookie)
		if c == nil || c.MaxAge >= 0 || c.Domain != "console.example.com" || !c.Secure {
			t.Fatalf("网关 cookie 应按主机名清除: %+v", c)
		}
	})

	t.Run("IP 访问时不写 Domain", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t, func(d *Deps) { d.DisablePassword = true })
		rec := h.do(t, http.MethodGet, "/__logout", "", func(r *http.Request) { r.Host = "10.0.0.7:5280" })
		if c := cookieNamed(rec, gatewayCookie); c == nil || c.Domain != "" {
			t.Fatalf("网关 cookie = %+v", c)
		}
	})

	t.Run("存储出错也要让用户退出", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.logoutErr = errors.New("db down")
		rec := h.do(t, http.MethodPost, "/__logout", "", sidCookie("abc"))
		if rec.Code != http.StatusSeeOther || cookieNamed(rec, httpx.SessionCookie) == nil {
			t.Fatalf("status=%d", rec.Code)
		}
	})
}

func TestRelogin(t *testing.T) {
	t.Parallel()
	h := newHarness(t)
	rec := h.do(t, http.MethodGet, "/__relogin", "")
	if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/" {
		t.Fatalf("status=%d location=%q", rec.Code, rec.Header().Get("Location"))
	}
	if c := cookieNamed(rec, httpx.LoggedOutCookie); c == nil || c.Value != "" || c.MaxAge >= 0 {
		t.Fatalf("退出标记应被清除: %+v", c)
	}
}

func TestMeErrors(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		err    error
		status int
		code   int
	}{
		{"未登录", domain.Wrap(domain.ErrUnauthorized, "auth", "登录已过期", nil), 401, httpx.CodeUnauthorized},
		{"被禁用", domain.Wrap(domain.ErrForbidden, "auth", "账号已禁用", nil), 403, httpx.CodeForbidden},
		{"内部错误", errors.New("sqlite: locked"), 500, httpx.CodeInternal},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := newHarness(t)
			h.sessions.authErr = tc.err
			rec := h.do(t, http.MethodGet, "/api/auth/me", "")
			env := wantFail(t, rec, tc.status, tc.code)
			if tc.status == 500 && (env.Message != "内部错误" || strings.Contains(rec.Body.String(), "sqlite")) {
				t.Fatalf("内部错误不得泄露原因: %s", rec.Body.String())
			}
		})
	}

	t.Run("项目清单读取失败", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t, func(d *Deps) { d.Tenants = fakeTenants{err: errors.New("db down")} })
		wantFail(t, h.do(t, http.MethodGet, "/api/auth/me", ""), http.StatusInternalServerError, httpx.CodeInternal)
	})
}

func TestMeProjects(t *testing.T) {
	t.Parallel()
	tenants := []domain.Tenant{
		{ID: "cube-b", Name: "Beta", Kind: domain.TenantKindPersonal},
		{ID: "cube-a"},
		{ID: "cube-off", Name: "Off", Disabled: true},
		{ID: "cube-x", Name: "Stranger"},
	}
	member := accountPrincipal()
	member.Roles = map[string]domain.Role{"cube-a": domain.RoleOwner, "cube-b": domain.RoleViewer, "cube-off": domain.RoleOwner}

	t.Run("普通账号只看到自己的未停用项目，按 ID 排序", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t, func(d *Deps) { d.Tenants = fakeTenants{list: tenants} })
		h.sessions.principal = member
		rec := h.do(t, http.MethodGet, "/api/auth/me", "")
		if rec.Code != http.StatusOK || rec.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("status=%d", rec.Code)
		}
		projects := at[[]any](t, decode(t, rec).Data["projects"])
		if len(projects) != 2 {
			t.Fatalf("projects = %v，期望 2 个", projects)
		}
		first := at[map[string]any](t, projects[0])
		if first["id"] != "cube-a" || first["name"] != "cube-a" || first["role"] != "owner" ||
			!at[bool](t, first["member"]) || first["kind"] != "team" {
			t.Fatalf("缺省名称取 ID、缺省类型是 team: %v", first)
		}
		second := at[map[string]any](t, projects[1])
		if second["id"] != "cube-b" || second["name"] != "Beta" || second["role"] != "viewer" || second["kind"] != "personal" {
			t.Fatalf("second = %v", second)
		}
	})

	t.Run("平台管理员能看到全部未停用项目，但 role 只报真实成员关系", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t, func(d *Deps) { d.Tenants = fakeTenants{list: tenants} })
		admin := member
		admin.PlatformAdmin = true
		h.sessions.principal = admin
		projects := at[[]any](t, decode(t, h.do(t, http.MethodGet, "/api/auth/me", "")).Data["projects"])
		if len(projects) != 3 {
			t.Fatalf("projects = %v，期望 3 个（停用的仍隐藏）", projects)
		}
		stranger := at[map[string]any](t, projects[2])
		if stranger["id"] != "cube-x" || stranger["role"] != "" || at[bool](t, stranger["member"]) {
			t.Fatalf("管理员对非成员项目不能显示成所有者: %v", stranger)
		}
	})
}

func TestSafeNext(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"":                    "/",
		"   ":                 "/",
		"/dash":               "/dash",
		" /dash?a=1#x ":       "/dash?a=1#x",
		"dash":                "/",
		"https://evil.com":    "/",
		"//evil.com":          "/",
		"/\\evil.com":         "/",
		"/\t/evil.com":        "/",
		"/ok\x7fbad":          "/",
		"javascript:alert(1)": "/",
	}
	for in, want := range cases {
		if got := safeNext(in); got != want {
			t.Errorf("safeNext(%q) = %q，期望 %q", in, got, want)
		}
	}
}
