package auth

import (
	"errors"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func TestNewRejectsIncompleteDeps(t *testing.T) {
	t.Parallel()
	ok := Deps{Sessions: &fakeSessions{}, Tenants: fakeTenants{}, Invites: &fakeInvites{}, SessionMaxLifetime: time.Hour}
	cases := map[string]func(*Deps){
		"缺 Sessions": func(d *Deps) { d.Sessions = nil },
		"缺 Tenants":  func(d *Deps) { d.Tenants = nil },
		"缺 Invites":  func(d *Deps) { d.Invites = nil },
		"会话上限为 0 会让登录 cookie 立即失效": func(d *Deps) { d.SessionMaxLifetime = 0 },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			deps := ok
			mutate(&deps)
			if _, err := New(deps); err == nil {
				t.Fatal("期望构造失败")
			}
		})
	}
	if _, err := New(ok); err != nil {
		t.Fatalf("完整依赖应构造成功: %v", err)
	}
}

func TestLoginFormSuccess(t *testing.T) {
	t.Parallel()
	h := newHarness(t)
	form := url.Values{"username": {"alice"}, "password": {"pw"}, "next": {"/dash?tab=1"}}.Encode()
	rec := h.do(t, http.MethodPost, "/__login", form, asForm())

	if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/dash?tab=1" {
		t.Fatalf("status=%d location=%q", rec.Code, rec.Header().Get("Location"))
	}
	if h.sessions.loginAs != "alice" || h.sessions.loginIP != "203.0.113.9" {
		t.Fatalf("Login 入参 = %q / %q", h.sessions.loginAs, h.sessions.loginIP)
	}
	c := cookieNamed(rec, httpx.SessionCookie)
	if c == nil || c.Value != "raw-session-secret" || !c.HttpOnly || !c.Secure ||
		c.SameSite != http.SameSiteStrictMode || c.MaxAge != 24*3600 || c.Path != "/" {
		t.Fatalf("会话 cookie 属性不符: %+v", c)
	}
	if rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("登录响应必须 no-store")
	}
}

func TestLoginRedirectNeverLeavesTheSite(t *testing.T) {
	t.Parallel()
	for _, next := range []string{"//evil.com", "https://evil.com", "/\\evil.com", "/\t/evil.com", "javascript:alert(1)"} {
		h := newHarness(t)
		form := url.Values{"username": {"alice"}, "password": {"pw"}, "next": {next}}.Encode()
		rec := h.do(t, http.MethodPost, "/__login", form, asForm())
		if loc := rec.Header().Get("Location"); loc != "/" {
			t.Errorf("next=%q 跳到了 %q", next, loc)
		}
	}
}

func TestLoginJSON(t *testing.T) {
	t.Parallel()
	h := newHarness(t)
	rec := h.do(t, http.MethodPost, "/__login", `{"username":"alice","password":"pw"}`, asJSON())
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d %s", rec.Code, rec.Body.String())
	}
	env := decode(t, rec)
	if env.Code != httpx.CodeOK || env.Data["username"] != "alice" || env.Data["currentProjectId"] != "cube-a" {
		t.Fatalf("信封 = %+v", env)
	}
	if _, ok := env.Data["projects"]; ok {
		t.Fatal("登录响应不带项目清单，清单走 /api/auth/me")
	}
	if cookieNamed(rec, httpx.SessionCookie) == nil || rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("JSON 登录同样要下发会话 cookie 且 no-store")
	}
}

func TestLoginFailures(t *testing.T) {
	t.Parallel()
	internal := errors.New("sqlite: database is locked")
	cases := []struct {
		name   string
		err    error
		status int
		code   int
		msg    string
	}{
		{"口令错误", domain.Wrap(domain.ErrUnauthorized, "login", "账号被禁用", nil), 401, httpx.CodeUnauthorized, msgLoginFailed},
		{"被限流", domain.Wrap(domain.ErrRateLimited, "login", "内部计数 5/5", nil), 429, httpx.CodeRateLimited, msgRateLimited},
		{"内部错误", internal, 500, httpx.CodeInternal, msgLoginBroken},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := newHarness(t)
			h.sessions.loginErr = tc.err
			rec := h.do(t, http.MethodPost, "/__login", `{"username":"alice","password":"pw"}`, asJSON())
			env := wantFail(t, rec, tc.status, tc.code)
			if env.Message != tc.msg {
				t.Fatalf("message = %q，期望固定文案 %q（不泄露内部原因）", env.Message, tc.msg)
			}
			if cookieNamed(rec, httpx.SessionCookie) != nil {
				t.Fatal("失败的登录不得下发会话 cookie")
			}
			if tc.status == 429 && rec.Header().Get("Retry-After") != "600" {
				t.Fatalf("Retry-After = %q，期望 600", rec.Header().Get("Retry-After"))
			}
			if strings.Contains(rec.Body.String(), "sqlite") || strings.Contains(rec.Body.String(), "5/5") {
				t.Fatalf("响应泄露了内部原因: %s", rec.Body.String())
			}
		})
	}

	t.Run("表单登录失败渲染登录页且不回显输入", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.loginErr = domain.Wrap(domain.ErrUnauthorized, "login", "", nil)
		form := url.Values{"username": {"<b>alice</b>"}, "password": {"hunter2"}, "next": {"/dash"}}.Encode()
		rec := h.do(t, http.MethodPost, "/__login", form, asForm())
		body := rec.Body.String()
		if rec.Code != http.StatusUnauthorized || !strings.Contains(body, msgLoginFailed) {
			t.Fatalf("status=%d body=%s", rec.Code, body)
		}
		if strings.Contains(body, "hunter2") || strings.Contains(body, "<b>alice") {
			t.Fatal("登录页不得回显用户名或口令")
		}
		if !strings.Contains(body, `name="next" value="/dash"`) {
			t.Fatal("失败后应保留（净化过的）next")
		}
		if !strings.Contains(rec.Header().Get("Content-Security-Policy"), "default-src 'none'") {
			t.Fatal("登录页必须带 CSP")
		}
	})

	t.Run("请求体不是合法 JSON", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		rec := h.do(t, http.MethodPost, "/__login", `{"username":`, asJSON())
		wantFail(t, rec, http.StatusBadRequest, httpx.CodeBadRequest)
		if h.sessions.loginAs != "" {
			t.Fatal("坏请求不应触达 Login")
		}
	})
}

func TestPasswordLoginDisabled(t *testing.T) {
	t.Parallel()
	h := newHarness(t, func(d *Deps) { d.DisablePassword = true })

	page := h.do(t, http.MethodGet, "/__login", "")
	if page.Code != http.StatusForbidden || !strings.Contains(page.Body.String(), "统一认证") {
		t.Fatalf("登录页 status=%d", page.Code)
	}
	rec := h.do(t, http.MethodPost, "/__login", `{"username":"alice","password":"pw"}`, asJSON())
	wantFail(t, rec, http.StatusForbidden, httpx.CodeForbidden)
	if h.sessions.loginAs != "" {
		t.Fatal("关闭账密登录后不得调用 Login")
	}
}

func TestLoginPage(t *testing.T) {
	t.Parallel()
	h := newHarness(t)
	rec := h.do(t, http.MethodGet, "/__login?next=//evil.com", "")
	body := rec.Body.String()
	if rec.Code != http.StatusOK || !strings.Contains(body, `<form`) {
		t.Fatalf("status=%d", rec.Code)
	}
	if strings.Contains(body, "evil.com") {
		t.Fatal("不安全的 next 不得写进页面")
	}
	if rec.Header().Get("X-Frame-Options") != "DENY" || rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("登录页需防嵌套并 no-store")
	}
}
