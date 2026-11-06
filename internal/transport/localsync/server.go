// Package localsync 提供 /api/local-sync/* 与浏览器授权页 /__device。
package localsync

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	svc "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/localsync"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Accounts 是会话认证端口，由 service/account 实现。
type Accounts interface {
	Authenticate(ctx context.Context, c domain.Credentials) (domain.Principal, error)
}

// Tenants 用来给授权页列出可选项目。
type Tenants interface {
	ListTenants(ctx context.Context) ([]domain.Tenant, error)
}

// Deps 是本地同步入口的依赖。
type Deps struct {
	Service  *svc.Service
	Accounts Accounts
	Tenants  Tenants
	// Enabled 为 false 时所有接口显式返回 503，不返回假空数据。
	Enabled bool
	// DisablePassword 关闭账密登录时，未登录访问授权页给出统一认证提示而不是跳登录页。
	DisablePassword bool
	Logger          *slog.Logger
}

// Server 提供设备授权、上传与授权页路由。
type Server struct {
	deps Deps
	log  *slog.Logger
	mux  *http.ServeMux
}

// New 构造本地同步入口。
func New(deps Deps) (*Server, error) {
	if deps.Service == nil || deps.Accounts == nil || deps.Tenants == nil {
		return nil, errors.New("localsync: Service/Accounts/Tenants 不能为空")
	}
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	s := &Server{deps: deps, log: deps.Logger, mux: http.NewServeMux()}
	s.register()
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		httpx.NotFoundJSON(w)
	})
	return s, nil
}

// Handler 返回挂到 console 角色的处理器。
func (s *Server) Handler() http.Handler { return httpx.MethodNotAllowedJSON(s.mux) }

func (s *Server) register() {
	s.mux.HandleFunc("POST /api/local-sync/device/code", s.createDeviceCode)
	s.mux.HandleFunc("POST /api/local-sync/device/token", s.pollDeviceToken)
	s.mux.HandleFunc("GET /api/local-sync/state", s.state)
	s.mux.HandleFunc("POST /api/local-sync/batches", s.createBatch)
	s.mux.HandleFunc("PUT /api/local-sync/batches/{batchId}/chunks/{seq}", s.putChunk)
	s.mux.HandleFunc("POST /api/local-sync/batches/{batchId}/commit", s.commitBatch)
	s.mux.HandleFunc("POST /api/local-sync/manual", s.manual)
	s.mux.HandleFunc("GET /__device", s.devicePage)
	s.mux.HandleFunc("POST /__device", s.deviceApprove)
}

// ensureEnabled 在能力关闭时写 503 并返回 false。
func (s *Server) ensureEnabled(w http.ResponseWriter) bool {
	if s.deps.Enabled {
		return true
	}
	httpx.Fail(w, http.StatusServiceUnavailable, httpx.CodeUnavailable, "本地同步能力未启用")
	return false
}

// device 解析设备令牌；每次请求都复核账号、成员关系与角色。
func (s *Server) device(w http.ResponseWriter, r *http.Request) (svc.DeviceIdentity, bool) {
	identity, err := s.deps.Service.AuthenticateDevice(r.Context(), httpx.BearerToken(r.Header.Get("Authorization")))
	if err != nil {
		s.fail(w, r, err)
		return svc.DeviceIdentity{}, false
	}
	return identity, true
}

// session 解析控制台会话；手工上传只接受账号主体，不接受管理 Token。
// 零信任外部身份同样是账号主体，但没有 SessionID，不能拿 SessionID 做判据。
func (s *Server) session(w http.ResponseWriter, r *http.Request) (domain.Principal, bool) {
	p, err := s.deps.Accounts.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		s.fail(w, r, err)
		return domain.Principal{}, false
	}
	if p.Kind != domain.PrincipalAccount || p.AccountID == "" {
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "手工上传需要账号身份，管理 Token 不支持")
		return domain.Principal{}, false
	}
	return p, true
}

func (s *Server) fail(w http.ResponseWriter, r *http.Request, err error) {
	status, code, message := mapError(err)
	if status == http.StatusTooManyRequests {
		httpx.SetRetryAfter(w, time.Minute)
	}
	if status >= http.StatusInternalServerError {
		s.log.ErrorContext(r.Context(), "local sync request failed", slog.String("error", err.Error()))
	}
	httpx.Fail(w, status, code, message)
}

func mapError(err error) (int, int, string) {
	switch {
	case errors.Is(err, httpx.ErrBodyTooLarge):
		// 与 Dashboard 同口径：请求体过大是 40000，41300 只留给配额超限。
		return http.StatusRequestEntityTooLarge, httpx.CodeBadRequest, "请求体过大"
	case errors.Is(err, domain.ErrQuotaExceeded):
		return http.StatusRequestEntityTooLarge, httpx.CodePayloadTooLarge, safeMessage(err, "项目配额超限")
	case errors.Is(err, httpx.ErrNotJSON):
		return http.StatusUnsupportedMediaType, httpx.CodeBadRequest, "Content-Type 必须是 application/json"
	case errors.Is(err, httpx.ErrBadJSON):
		return http.StatusBadRequest, httpx.CodeBadRequest, "请求体不是合法 JSON"
	case errors.Is(err, domain.ErrInvalidInput):
		return http.StatusBadRequest, httpx.CodeBadRequest, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrUnauthorized):
		return http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrForbidden):
		return http.StatusForbidden, httpx.CodeForbidden, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrNotFound):
		return http.StatusNotFound, httpx.CodeNotFound, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrRateLimited):
		return http.StatusTooManyRequests, httpx.CodeRateLimited, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrConflict):
		return http.StatusConflict, httpx.CodeConflict, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrDisabled), errors.Is(err, domain.ErrUpstreamUnavailable):
		return http.StatusServiceUnavailable, httpx.CodeUnavailable, domain.SafeMessage(err)
	default:
		return http.StatusInternalServerError, httpx.CodeInternal, "内部错误"
	}
}

func safeMessage(err error, fallback string) string {
	if domain.KindOf(err) == nil {
		return fallback
	}
	return domain.SafeMessage(err)
}

// verificationURI 返回插件展示给用户的授权页地址。配置优先；否则按请求推导。
// 末尾带上用户验证码，用户点开链接即可看到确认页。
func (s *Server) verificationURI(r *http.Request, userCode string) string {
	base := s.deps.Service.Options().VerificationURI
	if base == "" {
		scheme := "http"
		if r.TLS != nil || strings.EqualFold(headerFirst(r, "X-Forwarded-Proto"), "https") {
			scheme = "https"
		}
		host := r.Host
		if host == "" {
			host = "localhost"
		}
		base = scheme + "://" + host + "/__device"
	}
	separator := "?"
	if strings.Contains(base, "?") {
		separator = "&"
	}
	return base + separator + "code=" + url.QueryEscape(userCode)
}

func headerFirst(r *http.Request, name string) string {
	value := r.Header.Get(name)
	if index := strings.IndexByte(value, ','); index >= 0 {
		value = value[:index]
	}
	return strings.TrimSpace(value)
}
