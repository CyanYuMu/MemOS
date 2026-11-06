package auth

import (
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// 账号自服务只接受账号会话：管理 Token、统一认证（没有会话）都不能改口令或踢会话。
func TestSelfServiceRejectsNonSessionPrincipals(t *testing.T) {
	t.Parallel()
	adminToken := domain.Principal{Kind: domain.PrincipalAdminToken, Username: "admin"}
	external := accountPrincipal()
	external.SessionID = ""
	routes := []struct{ method, path, body string }{
		{http.MethodPost, "/api/auth/password", `{"oldPassword":"a","newPassword":"b"}`},
		{http.MethodGet, "/api/auth/sessions", ""},
		{http.MethodPost, "/api/auth/sessions/revoke", `{}`},
	}
	for _, rt := range routes {
		for name, p := range map[string]domain.Principal{"管理 Token": adminToken, "无会话的外部身份": external} {
			t.Run(rt.path+"/"+name, func(t *testing.T) {
				t.Parallel()
				h := newHarness(t)
				h.sessions.principal = p
				wantFail(t, h.do(t, rt.method, rt.path, rt.body, asJSON()), http.StatusBadRequest, httpx.CodeBadRequest)
				if h.sessions.changeArgs != nil || h.sessions.keepID != "" {
					t.Fatal("被拒绝的请求不得触达服务")
				}
			})
		}
		t.Run(rt.path+"/未登录", func(t *testing.T) {
			t.Parallel()
			h := newHarness(t)
			h.sessions.authErr = domain.Wrap(domain.ErrUnauthorized, "auth", "登录已过期", nil)
			wantFail(t, h.do(t, rt.method, rt.path, rt.body, asJSON()), http.StatusUnauthorized, httpx.CodeUnauthorized)
		})
	}
}

func TestChangePassword(t *testing.T) {
	t.Parallel()
	body := `{"oldPassword":"old-secret","newPassword":"new-secret"}`

	t.Run("成功", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		rec := h.do(t, http.MethodPost, "/api/auth/password", body, asJSON())
		if rec.Code != http.StatusOK || !at[bool](t, decode(t, rec).Data["ok"]) {
			t.Fatalf("status=%d %s", rec.Code, rec.Body.String())
		}
		want := []string{"acct-1", "sess-1", "old-secret", "new-secret"}
		if strings.Join(h.sessions.changeArgs, "|") != strings.Join(want, "|") {
			t.Fatalf("入参 = %v，期望账号/会话取自主体: %v", h.sessions.changeArgs, want)
		}
		if rec.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("改口令响应必须 no-store")
		}
	})

	errCases := []struct {
		name   string
		err    error
		status int
		code   int
	}{
		{"旧口令错误", domain.Wrap(domain.ErrUnauthorized, "pw", "旧口令不正确", nil), 401, httpx.CodeUnauthorized},
		{"新口令不合规", domain.Invalid("pw", "口令至少 8 位"), 400, httpx.CodeBadRequest},
		{"被限流", domain.Wrap(domain.ErrRateLimited, "pw", "尝试次数过多", nil), 429, httpx.CodeRateLimited},
		{"内部错误", errors.New("sqlite: locked"), 500, httpx.CodeInternal},
	}
	for _, tc := range errCases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := newHarness(t)
			h.sessions.changeErr = tc.err
			rec := h.do(t, http.MethodPost, "/api/auth/password", body, asJSON())
			env := wantFail(t, rec, tc.status, tc.code)
			if tc.status == 429 && rec.Header().Get("Retry-After") != "600" {
				t.Fatalf("Retry-After = %q", rec.Header().Get("Retry-After"))
			}
			if tc.status == 500 && (env.Message != "内部错误" || strings.Contains(rec.Body.String(), "sqlite")) {
				t.Fatalf("内部错误不得泄露原因: %s", rec.Body.String())
			}
			if strings.Contains(rec.Body.String(), "old-secret") || strings.Contains(rec.Body.String(), "new-secret") {
				t.Fatal("响应不得回显口令")
			}
		})
	}

	t.Run("请求必须是 JSON", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		wantFail(t, h.do(t, http.MethodPost, "/api/auth/password", "oldPassword=a", asForm()), http.StatusBadRequest, httpx.CodeBadRequest)
		wantFail(t, h.do(t, http.MethodPost, "/api/auth/password", `{"oldPassword":`, asJSON()), http.StatusBadRequest, httpx.CodeBadRequest)
		if h.sessions.changeArgs != nil {
			t.Fatal("格式错误的请求不得触达服务")
		}
	})
}

func TestListSessions(t *testing.T) {
	t.Parallel()
	seen := time.Date(2026, 10, 26, 6, 30, 0, 0, time.UTC)
	h := newHarness(t)
	h.sessions.sessions = []domain.Session{
		{
			ID: "sess-1", CurrentTenantID: "cube-a", LastSeenAt: &seen,
			CreatedAt: time.Date(2026, 10, 25, 0, 0, 0, 0, time.UTC), ExpiresAt: time.Date(2026, 11, 24, 0, 0, 0, 0, time.UTC),
		},
		{ID: "sess-2", CreatedAt: time.Date(2026, 10, 20, 16, 0, 0, 0, time.UTC), ExpiresAt: time.Date(2026, 11, 19, 16, 0, 0, 0, time.UTC)},
	}
	rec := h.do(t, http.MethodGet, "/api/auth/sessions", "")
	if rec.Code != http.StatusOK || rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("status=%d", rec.Code)
	}
	data := decode(t, rec).Data
	if data["total"] != float64(2) {
		t.Fatalf("total = %v", data["total"])
	}
	rows := at[[]any](t, data["sessions"])
	cur, other := at[map[string]any](t, rows[0]), at[map[string]any](t, rows[1])
	if !at[bool](t, cur["current"]) || at[bool](t, other["current"]) {
		t.Fatalf("current 标记应只落在本会话: %v / %v", cur["current"], other["current"])
	}
	// 展示口径是北京时间。
	if cur["lastSeenAt"] != "2026-10-26 14:30:00" || cur["createdAt"] != "2026-10-25 08:00:00" || cur["projectId"] != "cube-a" {
		t.Fatalf("cur = %v", cur)
	}
	if other["lastSeenAt"] != "" || other["createdAt"] != "2026-10-21 00:00:00" {
		t.Fatalf("从未使用的会话 lastSeenAt 为空串: %v", other)
	}
	for _, leaked := range []string{"Hash", "hash", "RevokedAt"} {
		if strings.Contains(rec.Body.String(), leaked) {
			t.Fatalf("响应不应暴露内部字段 %s", leaked)
		}
	}
}

func TestListSessionsInternalError(t *testing.T) {
	t.Parallel()
	h := newHarness(t)
	h.sessions.sessionsErr = errors.New("sqlite: locked")
	rec := h.do(t, http.MethodGet, "/api/auth/sessions", "")
	env := wantFail(t, rec, http.StatusInternalServerError, httpx.CodeInternal)
	if env.Message != "内部错误" {
		t.Fatalf("message = %q", env.Message)
	}
}

func TestRevokeOtherSessions(t *testing.T) {
	t.Parallel()
	t.Run("保留本会话", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.revoked = 3
		rec := h.do(t, http.MethodPost, "/api/auth/sessions/revoke", `{}`, asJSON())
		if rec.Code != http.StatusOK || decode(t, rec).Data["revoked"] != float64(3) {
			t.Fatalf("status=%d %s", rec.Code, rec.Body.String())
		}
		if h.sessions.keepID != "sess-1" {
			t.Fatalf("keepID = %q，应保留当前会话", h.sessions.keepID)
		}
	})
	t.Run("失败", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.revokeErr = errors.New("db down")
		wantFail(t, h.do(t, http.MethodPost, "/api/auth/sessions/revoke", `{}`, asJSON()), http.StatusInternalServerError, httpx.CodeInternal)
	})
	t.Run("带 body 时必须是 JSON", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		wantFail(t, h.do(t, http.MethodPost, "/api/auth/sessions/revoke", "x=1", asForm()), http.StatusBadRequest, httpx.CodeBadRequest)
		if h.sessions.keepID != "" {
			t.Fatal("被拒绝的请求不得触达服务")
		}
	})
}
