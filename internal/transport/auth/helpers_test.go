package auth

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
)

// fakeSessions 记录调用参数；默认返回已登录的账号会话主体。
type fakeSessions struct {
	principal domain.Principal
	authErr   error

	loginRaw string
	loginErr error
	loginIP  string
	loginAs  string

	logoutName string
	logoutErr  error
	logoutRaw  string

	changeErr  error
	changeArgs []string

	sessions    []domain.Session
	sessionsErr error

	revoked   int64
	revokeErr error
	keepID    string
}

func (f *fakeSessions) Login(_ context.Context, username, _, ip string) (string, domain.Session, error) {
	f.loginAs, f.loginIP = username, ip
	return f.loginRaw, domain.Session{}, f.loginErr
}

func (f *fakeSessions) Logout(_ context.Context, raw string) (string, error) {
	f.logoutRaw = raw
	return f.logoutName, f.logoutErr
}

func (f *fakeSessions) Authenticate(context.Context, domain.Credentials) (domain.Principal, error) {
	return f.principal, f.authErr
}

func (f *fakeSessions) ChangePassword(_ context.Context, in account.PasswordChangeInput) error {
	f.changeArgs = []string{in.AccountID, in.SessionID, in.OldPassword, in.NewPassword}
	return f.changeErr
}

func (f *fakeSessions) Sessions(context.Context, string) ([]domain.Session, error) {
	return f.sessions, f.sessionsErr
}

func (f *fakeSessions) RevokeOtherSessions(_ context.Context, _, keepID string) (int64, error) {
	f.keepID = keepID
	return f.revoked, f.revokeErr
}

type fakeTenants struct {
	list []domain.Tenant
	err  error
}

func (f fakeTenants) List(context.Context) ([]domain.Tenant, error) { return f.list, f.err }

type fakeInvites struct {
	preview    account.InvitePreview
	previewErr error
	redeemed   domain.ProjectInvite
	redeemErr  error
	redeems    int
	previews   int
}

func (f *fakeInvites) PreviewInvite(context.Context, domain.Principal, string) (account.InvitePreview, error) {
	f.previews++
	return f.preview, f.previewErr
}

func (f *fakeInvites) RedeemInvite(context.Context, domain.Principal, string) (domain.ProjectInvite, error) {
	f.redeems++
	return f.redeemed, f.redeemErr
}

// accountPrincipal 是一个带会话的普通账号主体。
func accountPrincipal() domain.Principal {
	return domain.Principal{
		Kind: domain.PrincipalAccount, AccountID: "acct-1", Username: "alice", DisplayName: "Alice",
		SessionID: "sess-1", CurrentTenant: "cube-a",
		Roles: map[string]domain.Role{"cube-a": domain.RoleOwner},
	}
}

type harness struct {
	srv      *Server
	sessions *fakeSessions
	invites  *fakeInvites
}

func newHarness(t *testing.T, mutate ...func(*Deps)) *harness {
	t.Helper()
	h := &harness{
		sessions: &fakeSessions{principal: accountPrincipal(), loginRaw: "raw-session-secret"},
		invites:  &fakeInvites{},
	}
	deps := Deps{
		Sessions:           h.sessions,
		Tenants:            fakeTenants{},
		Invites:            h.invites,
		SessionTTL:         12 * time.Hour,
		SessionMaxLifetime: 24 * time.Hour,
		CookieSecure:       true,
		LoginFailWindow:    10 * time.Minute,
		Logger:             slog.New(slog.DiscardHandler),
	}
	for _, m := range mutate {
		m(&deps)
	}
	srv, err := New(deps)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	h.srv = srv
	return h
}

type reqOpt func(*http.Request)

func header(k, v string) reqOpt { return func(r *http.Request) { r.Header.Set(k, v) } }

func asJSON() reqOpt { return header("Content-Type", "application/json") }

func asForm() reqOpt { return header("Content-Type", "application/x-www-form-urlencoded") }

func (h *harness) do(t *testing.T, method, target, body string, opts ...reqOpt) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequestWithContext(t.Context(), method, target, strings.NewReader(body))
	req.RemoteAddr = "203.0.113.9:4321"
	for _, o := range opts {
		o(req)
	}
	rec := httptest.NewRecorder()
	h.srv.Handler().ServeHTTP(rec, req)
	return rec
}

type envelope struct {
	Code    int            `json:"code"`
	Data    map[string]any `json:"data"`
	Message string         `json:"message"`
}

// at 断言 JSON 解码后的动态值类型；类型不符直接失败，免得后面的下标越界掩盖真正的问题。
func at[T any](t *testing.T, v any) T {
	t.Helper()
	out, ok := v.(T)
	if !ok {
		t.Fatalf("值 %v 的类型是 %T，不是期望的 %T", v, v, out)
	}
	return out
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) envelope {
	t.Helper()
	var env envelope
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("响应不是信封 JSON: %v\n%s", err, rec.Body.String())
	}
	return env
}

func cookieNamed(rec *httptest.ResponseRecorder, name string) *http.Cookie {
	for _, c := range rec.Result().Cookies() {
		if c.Name == name {
			return c
		}
	}
	return nil
}

func wantFail(t *testing.T, rec *httptest.ResponseRecorder, status, code int) envelope {
	t.Helper()
	if rec.Code != status {
		t.Fatalf("status = %d，期望 %d\n%s", rec.Code, status, rec.Body.String())
	}
	env := decode(t, rec)
	if env.Code != code || env.Data != nil {
		t.Fatalf("信封 code=%d data=%v，期望 code=%d data=null", env.Code, env.Data, code)
	}
	return env
}
