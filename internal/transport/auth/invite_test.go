package auth

import (
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
)

const inviteToken = "inv_SECRET-token-123"

func previewFor(name string, role domain.Role, already bool) account.InvitePreview {
	return account.InvitePreview{
		Invite:        domain.ProjectInvite{Role: role, ExpiresAt: time.Date(2026, 11, 2, 0, 0, 0, 0, time.UTC)},
		Tenant:        domain.Tenant{ID: "cube-a", Name: name},
		AlreadyMember: already,
	}
}

func TestInviteLandingNeverConsumes(t *testing.T) {
	t.Parallel()
	h := newHarness(t)
	h.invites.preview = previewFor("Alpha", domain.RoleOwner, false)
	rec := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "")
	body := rec.Body.String()

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d %s", rec.Code, body)
	}
	if h.invites.redeems != 0 || h.invites.previews != 1 {
		t.Fatalf("GET 只能预览：redeems=%d previews=%d（聊天工具会预抓取链接）", h.invites.redeems, h.invites.previews)
	}
	for _, want := range []string{"Alpha", "管理员（owner）", "2026-11-02 08:00", `method="post"`} {
		if !strings.Contains(body, want) {
			t.Errorf("确认页缺少 %q", want)
		}
	}
	if strings.Contains(body, inviteToken) {
		t.Error("页面不得回显邀请 token")
	}
	hdr := rec.Header()
	if hdr.Get("Cache-Control") != "no-store" || hdr.Get("X-Frame-Options") != "DENY" ||
		hdr.Get("Referrer-Policy") != "no-referrer" {
		t.Errorf("确认页安全头缺失: %v", hdr)
	}
	if csp := hdr.Get("Content-Security-Policy"); !strings.Contains(csp, "default-src 'none'") || !strings.Contains(csp, "form-action 'self'") {
		t.Errorf("CSP = %q", csp)
	}
}

func TestInviteLandingVariants(t *testing.T) {
	t.Parallel()
	t.Run("已是成员只给进入链接", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.invites.preview = previewFor("Alpha", domain.RoleViewer, true)
		body := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "").Body.String()
		if !strings.Contains(body, "你已加入该项目") || !strings.Contains(body, `href="/"`) || strings.Contains(body, "<form") {
			t.Fatalf("body = %s", body)
		}
	})
	t.Run("项目名按 HTML 转义", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.invites.preview = previewFor(`<script>alert(1)</script>`, domain.RoleEditor, false)
		body := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "").Body.String()
		if strings.Contains(body, "<script>alert(1)") || !strings.Contains(body, "编辑者（editor）") {
			t.Fatalf("body = %s", body)
		}
	})
	t.Run("项目没有名字时显示 ID", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.invites.preview = previewFor("  ", domain.RoleViewer, false)
		if body := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "").Body.String(); !strings.Contains(body, "cube-a") {
			t.Fatalf("body = %s", body)
		}
	})
	t.Run("邀请无效统一说无效，不区分原因也不回显 token", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.invites.previewErr = domain.NotFound("invite", "邀请已被撤销")
		rec := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "")
		body := rec.Body.String()
		if rec.Code != http.StatusNotFound || !strings.Contains(body, msgInviteInvalid) {
			t.Fatalf("status=%d", rec.Code)
		}
		if strings.Contains(body, inviteToken) || strings.Contains(body, "撤销") {
			t.Fatalf("页面泄露了 token 或具体原因: %s", body)
		}
	})
}

func TestInviteNeedsLogin(t *testing.T) {
	t.Parallel()
	unauth := domain.Wrap(domain.ErrUnauthorized, "auth", "未登录", nil)

	t.Run("账密模式跳登录页并带回跳地址", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.authErr = unauth
		rec := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "")
		want := "/__login?next=%2F__invite%2F" + inviteToken
		if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != want {
			t.Fatalf("status=%d location=%q", rec.Code, rec.Header().Get("Location"))
		}
	})
	t.Run("统一认证模式说明要走网关", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t, func(d *Deps) { d.DisablePassword = true })
		h.sessions.authErr = unauth
		rec := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "")
		if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "统一认证网关") {
			t.Fatalf("status=%d", rec.Code)
		}
	})
	t.Run("账号被禁用等非未登录错误不引导去登录", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.authErr = domain.Wrap(domain.ErrForbidden, "auth", "账号已禁用", nil)
		rec := h.do(t, http.MethodGet, "/__invite/"+inviteToken, "")
		if rec.Code != http.StatusForbidden || strings.Contains(rec.Body.String(), "禁用") {
			t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
		}
	})
	t.Run("POST 未登录同样跳登录", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.sessions.authErr = unauth
		rec := h.do(t, http.MethodPost, "/__invite/"+inviteToken, "")
		if rec.Code != http.StatusSeeOther || h.invites.redeems != 0 {
			t.Fatalf("status=%d redeems=%d", rec.Code, h.invites.redeems)
		}
	})
}

func TestInviteRedeem(t *testing.T) {
	t.Parallel()
	t.Run("成功后回首页", func(t *testing.T) {
		t.Parallel()
		h := newHarness(t)
		h.invites.redeemed = domain.ProjectInvite{ID: "inv-1", TenantID: "cube-a", Role: domain.RoleEditor}
		rec := h.do(t, http.MethodPost, "/__invite/"+inviteToken, "", header("Sec-Fetch-Site", "same-origin"))
		if rec.Code != http.StatusSeeOther || rec.Header().Get("Location") != "/" || h.invites.redeems != 1 {
			t.Fatalf("status=%d location=%q redeems=%d", rec.Code, rec.Header().Get("Location"), h.invites.redeems)
		}
	})
	t.Run("兑换失败的状态码", func(t *testing.T) {
		t.Parallel()
		cases := []struct {
			err  error
			want int
		}{
			{domain.Wrap(domain.ErrForbidden, "invite", "账号已禁用", nil), http.StatusForbidden},
			{domain.NotFound("invite", "邀请已用过"), http.StatusNotFound},
			{errors.New("sqlite: locked"), http.StatusNotFound},
		}
		for _, tc := range cases {
			h := newHarness(t)
			h.invites.redeemErr = tc.err
			rec := h.do(t, http.MethodPost, "/__invite/"+inviteToken, "")
			body := rec.Body.String()
			if rec.Code != tc.want || !strings.Contains(body, msgInviteInvalid) {
				t.Errorf("err=%v status=%d，期望 %d", tc.err, rec.Code, tc.want)
			}
			if strings.Contains(body, inviteToken) || strings.Contains(body, "sqlite") || strings.Contains(body, "已用过") {
				t.Errorf("页面泄露内部原因或 token: %s", body)
			}
		}
	})
	t.Run("跨站提交被拒且不兑换", func(t *testing.T) {
		t.Parallel()
		for name, opt := range map[string]reqOpt{
			"Sec-Fetch-Site":   header("Sec-Fetch-Site", "cross-site"),
			"same-site 子域":     header("Sec-Fetch-Site", "same-site"),
			"Origin 与 Host 不符": header("Origin", "https://evil.example"),
		} {
			h := newHarness(t)
			rec := h.do(t, http.MethodPost, "/__invite/"+inviteToken, "", opt)
			if rec.Code != http.StatusForbidden || h.invites.redeems != 0 {
				t.Errorf("%s: status=%d redeems=%d", name, rec.Code, h.invites.redeems)
			}
		}
	})
}
