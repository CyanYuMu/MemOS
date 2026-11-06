package auth

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"slices"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/onboarding"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Sessions 是登录入口需要的账号服务端口。
type Sessions interface {
	Login(ctx context.Context, username, password, remoteIP string) (string, domain.Session, error)
	Logout(ctx context.Context, rawSession string) (username string, err error)
	Authenticator
	ChangePassword(ctx context.Context, in account.PasswordChangeInput) error
	Sessions(ctx context.Context, accountID string) ([]domain.Session, error)
	RevokeOtherSessions(ctx context.Context, accountID, keepID string) (int64, error)
}

// Invites 是邀请落地页需要的账号服务端口。
type Invites interface {
	PreviewInvite(ctx context.Context, actor domain.Principal, rawToken string) (account.InvitePreview, error)
	RedeemInvite(ctx context.Context, actor domain.Principal, rawToken string) (domain.ProjectInvite, error)
}

// Tenants 用来给 /api/auth/me 补项目名称。
type Tenants interface {
	List(ctx context.Context) ([]domain.Tenant, error)
}

// Deps 是登录入口的依赖。
type Deps struct {
	Onboarding *onboarding.Service
	Sessions   Sessions
	Tenants    Tenants
	Invites    Invites
	Audit      *audit.Recorder
	SessionTTL time.Duration
	// SessionMaxLifetime 是会话的绝对上限，也是登录 cookie 的 MaxAge：
	// 服务端按 expires_at 拒绝过期会话，cookie 只决定浏览器何时丢弃它。
	SessionMaxLifetime time.Duration
	CookieSecure       bool
	// LoginFailWindow 是登录失败节流窗口，用于 429 的 Retry-After；零值不设该头。
	LoginFailWindow time.Duration
	// DisablePassword 关闭 /__login 的账密登录，只允许统一认证入口。
	DisablePassword bool
	// LogoutURL 非空时，登出后跳到这里（通常是 UUAP 的退出页）。
	LogoutURL string
	Logger    *slog.Logger
}

const gatewayCookie = "SECURE_ZT_GW_TOKEN"

// Server 提供 /__login、/__logout 与 /api/auth/me。
type Server struct {
	deps Deps
	mux  *http.ServeMux
}

const (
	maxFormBytes     = 8 << 10
	maxAuditUsername = 64
)

// New 构造登录入口。
func New(deps Deps) (*Server, error) {
	if deps.Sessions == nil || deps.Tenants == nil || deps.Invites == nil {
		return nil, errors.New("auth: Sessions/Tenants/Invites 不能为空")
	}
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	if deps.SessionMaxLifetime <= 0 {
		// 0 会把 cookie 的 MaxAge 写成 0，浏览器直接当会话 cookie 丢掉；
		// 配置层已经保证它 >= SessionTTL，这里只拦“忘了传”。
		return nil, errors.New("auth: SessionMaxLifetime 必须大于 0")
	}
	s := &Server{deps: deps, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /__login", s.loginPage)
	s.mux.HandleFunc("POST /__login", s.login)
	s.mux.HandleFunc("GET /__logout", s.logout)
	s.mux.HandleFunc("POST /__logout", s.logout)
	s.mux.HandleFunc("GET /__relogin", s.relogin)
	s.mux.HandleFunc("GET /__invite/{token}", s.inviteGET)
	s.mux.HandleFunc("POST /__invite/{token}", s.invitePOST)
	s.mux.HandleFunc("GET /api/auth/me", s.me)
	s.mux.HandleFunc("GET /api/auth/onboarding", s.onboarding)
	s.mux.HandleFunc("PUT /api/auth/onboarding", s.onboarding)
	s.mux.HandleFunc("POST /api/auth/password", s.changePassword)
	s.mux.HandleFunc("GET /api/auth/sessions", s.listSessions)
	s.mux.HandleFunc("POST /api/auth/sessions/revoke", s.revokeOtherSessions)
	// 未知路径也回 JSON 信封；Go 1.22 会优先匹配更具体的 pattern。
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		httpx.NotFoundJSON(w)
	})
	return s, nil
}

// Handler 返回登录入口的路由，挂在 console 角色，不需要凭据。
func (s *Server) Handler() http.Handler { return httpx.MethodNotAllowedJSON(s.mux) }

func (s *Server) loginPage(w http.ResponseWriter, r *http.Request) {
	if s.deps.DisablePassword {
		writeLoginPage(w, http.StatusForbidden, "已启用统一认证，请通过零信任网关访问", r.URL.Query().Get("next"))
		return
	}
	writeLoginPage(w, http.StatusOK, "", r.URL.Query().Get("next"))
}

type loginInput struct{ username, password, next string }

func readLogin(w http.ResponseWriter, r *http.Request) (loginInput, bool, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxFormBytes)
	if strings.HasPrefix(r.Header.Get("Content-Type"), "application/json") {
		var body struct {
			Username string `json:"username"`
			Password string `json:"password"`
		}
		if err := httpx.DecodeJSON(r, &body); err != nil {
			return loginInput{}, true, err
		}
		// JSON 登录不跟随 next：调用方通常是脚本，跳转没有意义。
		return loginInput{username: body.Username, password: body.Password}, true, nil
	}
	if err := r.ParseForm(); err != nil {
		return loginInput{}, false, httpx.ErrBadJSON
	}
	return loginInput{
		username: r.PostForm.Get("username"),
		password: r.PostForm.Get("password"),
		next:     r.PostForm.Get("next"),
	}, false, nil
}

func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	if s.deps.DisablePassword {
		s.loginFailure(w, loginFailureInfo{
			AsJSON:  true,
			Status:  http.StatusForbidden,
			Code:    httpx.CodeForbidden,
			Message: "已启用统一认证，请通过零信任网关访问",
			Next:    "",
		})
		return
	}
	in, asJSON, err := readLogin(w, r)
	if err != nil {
		s.loginFailure(w, loginFailureInfo{
			AsJSON:  asJSON,
			Status:  http.StatusBadRequest,
			Code:    httpx.CodeBadRequest,
			Message: "请求格式不正确",
			Next:    "",
		})
		return
	}
	ip := remoteIP(r)
	raw, _, err := s.deps.Sessions.Login(r.Context(), in.username, in.password, ip)
	s.audit(r.Context(), authAudit{Tool: "auth.login", Username: in.username, IP: ip, Err: err})
	if err != nil {
		s.loginError(r.Context(), w, loginErrorInfo{AsJSON: asJSON, Next: in.next, Err: err})
		return
	}
	s.setCookie(w, raw, int(s.deps.SessionMaxLifetime/time.Second))
	if !asJSON {
		setPageHeaders(w)
		http.Redirect(w, r, safeNext(in.next), http.StatusSeeOther)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	p, err := s.deps.Sessions.Authenticate(r.Context(), domain.Credentials{Session: raw})
	if err != nil {
		s.loginFailure(w, loginFailureInfo{
			AsJSON:  true,
			Status:  http.StatusInternalServerError,
			Code:    httpx.CodeInternal,
			Message: msgLoginBroken,
			Next:    "",
		})
		return
	}
	httpx.OK(w, meJSON(p, nil))
}

// loginErrorInfo 是登录失败的响应入参。
type loginErrorInfo struct {
	AsJSON bool
	Next   string
	Err    error
}

func (s *Server) loginError(ctx context.Context, w http.ResponseWriter, in loginErrorInfo) {
	switch {
	case errors.Is(in.Err, domain.ErrRateLimited):
		s.loginFailure(w, loginFailureInfo{
			AsJSON:  in.AsJSON,
			Next:    in.Next,
			Status:  http.StatusTooManyRequests,
			Code:    httpx.CodeRateLimited,
			Message: msgRateLimited,
		})
	case errors.Is(in.Err, domain.ErrUnauthorized):
		s.loginFailure(w, loginFailureInfo{
			AsJSON:  in.AsJSON,
			Next:    in.Next,
			Status:  http.StatusUnauthorized,
			Code:    httpx.CodeUnauthorized,
			Message: msgLoginFailed,
		})
	default:
		s.deps.Logger.ErrorContext(ctx, "login failed", slog.Any("err", in.Err))
		s.loginFailure(w, loginFailureInfo{
			AsJSON:  in.AsJSON,
			Next:    in.Next,
			Status:  http.StatusInternalServerError,
			Code:    httpx.CodeInternal,
			Message: msgLoginBroken,
		})
	}
}

// loginFailureInfo 是登录失败的响应入参。
type loginFailureInfo struct {
	AsJSON  bool
	Status  int
	Code    int
	Message string
	Next    string
}

func (s *Server) loginFailure(w http.ResponseWriter, in loginFailureInfo) {
	status, asJSON, code, message, next := in.Status, in.AsJSON, in.Code, in.Message, in.Next
	if status == http.StatusTooManyRequests {
		httpx.SetRetryAfter(w, s.deps.LoginFailWindow)
	}
	if asJSON {
		w.Header().Set("Cache-Control", "no-store")
		httpx.Fail(w, status, code, message)
		return
	}
	writeLoginPage(w, status, message, next)
}

func (s *Server) logout(w http.ResponseWriter, r *http.Request) {
	creds := httpx.CredentialsOf(r)
	username, err := s.deps.Sessions.Logout(r.Context(), creds.Session)
	if username != "" {
		s.audit(r.Context(), authAudit{Tool: "auth.logout", Username: username, IP: remoteIP(r), Err: err})
	}
	if err != nil {
		s.deps.Logger.ErrorContext(r.Context(), "logout failed", slog.Any("err", err))
	}
	s.setCookie(w, "", -1)
	s.setLoggedOut(w, true)
	target := "/"
	if s.deps.LogoutURL != "" || s.deps.DisablePassword {
		s.clearGatewayCookie(w, r)
	}
	if s.deps.LogoutURL != "" {
		target = s.deps.LogoutURL
	}
	setPageHeaders(w)
	http.Redirect(w, r, target, http.StatusSeeOther)
}

// relogin 是“已退出登录”页面上的按钮：只清退出标记，身份仍由网关/登录页决定。
func (s *Server) relogin(w http.ResponseWriter, r *http.Request) {
	s.setLoggedOut(w, false)
	setPageHeaders(w)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

// setLoggedOut 写入/清除“已主动退出”标记。标记是会话级 cookie：关闭浏览器即失效，
// 不会把用户永久锁在退出页。HttpOnly 防止前端脚本误清。
func (s *Server) setLoggedOut(w http.ResponseWriter, on bool) {
	//nolint:gosec // Secure 跟部署配置走：默认开，纯内网 http 联调时显式关（与登录 cookie 同口径）
	c := &http.Cookie{
		Name:     httpx.LoggedOutCookie,
		Path:     "/",
		HttpOnly: true,
		Secure:   s.deps.CookieSecure,
		SameSite: http.SameSiteLaxMode,
	}
	if on {
		c.Value = "1"
	} else {
		c.Value = ""
		c.MaxAge = -1
	}
	http.SetCookie(w, c)
}

func (s *Server) clearGatewayCookie(w http.ResponseWriter, r *http.Request) {
	host := r.Host
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	c := &http.Cookie{
		Name:     gatewayCookie,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   true,
		SameSite: http.SameSiteLaxMode,
	}
	if host != "" && net.ParseIP(host) == nil {
		c.Domain = host
	}
	http.SetCookie(w, c)
}

func (s *Server) setCookie(w http.ResponseWriter, value string, maxAge int) {
	//nolint:gosec // Secure 由部署配置决定：默认开，纯内网 http 联调显式关
	http.SetCookie(w, &http.Cookie{
		Name:     httpx.SessionCookie,
		Value:    value,
		Path:     "/",
		MaxAge:   maxAge,
		HttpOnly: true,
		Secure:   s.deps.CookieSecure,
		SameSite: http.SameSiteStrictMode,
	})
}

func (s *Server) me(w http.ResponseWriter, r *http.Request) {
	p, err := s.deps.Sessions.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		switch {
		case errors.Is(err, domain.ErrUnauthorized):
			httpx.Fail(w, http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err))
		case errors.Is(err, domain.ErrForbidden):
			httpx.Fail(w, http.StatusForbidden, httpx.CodeForbidden, domain.SafeMessage(err))
		default:
			s.deps.Logger.ErrorContext(r.Context(), "auth me failed", slog.Any("err", err))
			httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "内部错误")
		}
		return
	}
	tenants, err := s.deps.Tenants.List(r.Context())
	if err != nil {
		s.deps.Logger.ErrorContext(r.Context(), "auth me tenants failed", slog.Any("err", err))
		httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "内部错误")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	httpx.OK(w, meJSON(p, tenants))
}

// meJSON 是 /api/auth/me 与 JSON 登录的响应体。tenants 为 nil 时不带项目清单。
func meJSON(p domain.Principal, tenants []domain.Tenant) map[string]any {
	out := map[string]any{
		"accountId":        p.AccountID,
		"username":         p.Username,
		"displayName":      p.DisplayName,
		"platformAdmin":    p.PlatformAdmin,
		"currentProjectId": p.CurrentTenant,
	}
	if tenants == nil {
		return out
	}
	sorted := slices.Clone(tenants)
	slices.SortFunc(sorted, func(a, b domain.Tenant) int { return strings.Compare(a.ID, b.ID) })
	projects := make([]map[string]any, 0, len(sorted))
	for i := range sorted {
		// 平台管理员也能看到全部项目，但 role 只报真实成员关系：RoleIn 对管理员
		// 恒为 owner，直接用它会让前端把管理员显示成每个项目的所有者。
		role := p.MembershipIn(sorted[i].ID)
		if sorted[i].Disabled {
			continue
		}
		if role == "" && !p.PlatformAdmin {
			continue
		}
		name := sorted[i].Name
		if name == "" {
			name = sorted[i].ID
		}
		kind := sorted[i].Kind
		if kind == "" {
			kind = domain.TenantKindTeam
		}
		projects = append(projects, map[string]any{
			"id": sorted[i].ID, "name": name, "role": string(role), "member": role != "", "kind": string(kind),
		})
	}
	out["projects"] = projects
	return out
}

// authAudit 是认证动作的审计入参；Input 里不得出现凭据。
type authAudit struct {
	Tool     string
	Username string
	IP       string
	Input    map[string]any
	Err      error
}

// audit 记录一次认证/自服务动作：只含用户名、来源 IP 与错误类型，不含凭据。
// 来源 IP 进一等字段（不再塞进 Input），便于按 IP 检索。
func (s *Server) audit(ctx context.Context, e authAudit) {
	e.Input = map[string]any{"username": e.Username}
	s.auditWithInput(ctx, e)
}

// auditWithInput 是带额外字段的审计入口；调用方负责保证 Input 里没有凭据。
func (s *Server) auditWithInput(ctx context.Context, e authAudit) {
	if s.deps.Audit == nil {
		return
	}
	tool, username, ip, input, err := e.Tool, e.Username, e.IP, e.Input, e.Err
	username = domain.TruncateRunes(username, maxAuditUsername)
	rec := domain.AuditRecord{
		ID: domain.NewID(), CallID: domain.NewID(), TS: time.Now(), Tool: tool,
		Source: domain.AuditSourceAuth,
		UserID: username, RemoteIP: ip, OK: err == nil,
		Input: input,
	}
	if err != nil {
		if kind := domain.KindOf(err); kind != nil {
			rec.Err = kind.Error()
		} else {
			rec.Err = "internal"
		}
	}
	s.deps.Audit.Record(ctx, rec)
}

func remoteIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
