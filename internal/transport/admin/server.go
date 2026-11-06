// 凭据可以是管理 Token（Bearer）或账号会话，按角色矩阵授权。
// 包级说明见 doc.go。
package admin

import (
	"context"
	"errors"
	"log/slog"
	"maps"
	"net/http"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Tenants 是项目注册表端口，由 service/tenant 实现；命名与保留前缀等规则都在服务里。
type Tenants interface {
	Get(ctx context.Context, id string) (domain.Tenant, error)
	List(ctx context.Context) ([]domain.Tenant, error)
	Upsert(ctx context.Context, in tenant.UpsertInput) (domain.Tenant, error)
}

// Jobs 是死信排查与人工重放端口，由 service/jobs 实现；重放是否安全由服务判定。
type Jobs interface {
	List(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error)
	Retry(ctx context.Context, id string, force bool) (domain.Job, error)
}

// Accounts 是认证与账号、成员管理端口，由 account 服务实现。
type Accounts interface {
	Authenticate(ctx context.Context, c domain.Credentials) (domain.Principal, error)
	CreateAccount(ctx context.Context, in account.CreateAccountInput) (domain.Account, error)
	UpdateAccount(ctx context.Context, actorAccountID, id string, in account.UpdateAccountInput) (domain.Account, error)
	ResetPassword(ctx context.Context, id, password string) error
	ListAccounts(ctx context.Context) ([]domain.Account, error)
	ListAccountsPage(ctx context.Context, query string, page, size int) ([]domain.Account, int, error)
	GetAccount(ctx context.Context, id string) (domain.Account, error)
	ResolveAccount(ctx context.Context, idOrUsername string) (domain.Account, error)
	SetMember(ctx context.Context, in account.MemberInput) error
	RemoveMember(ctx context.Context, actorAccountID, tenantID, accountID string) error
	LeaveProject(ctx context.Context, accountID, tenantID string) error
	ListMembers(ctx context.Context, tenantID string) ([]domain.Membership, error)
	MemberCandidates(ctx context.Context, actor domain.Principal, tenantID, query string) ([]domain.MemberCandidate, error)
	RevokeSessions(ctx context.Context, accountID string) error
	DeleteAccount(ctx context.Context, actorAccountID, id string) error

	CreateInvite(ctx context.Context, in account.CreateInviteInput) (account.CreatedInvite, error)
	ListInvites(ctx context.Context, actor domain.Principal, tenantID string) ([]domain.ProjectInvite, error)
	RevokeInvite(ctx context.Context, actor domain.Principal, tenantID, inviteID string) error
	UpdateInviteRole(ctx context.Context, in account.InviteRoleInput) error
}

// Deps 是 admin 入口的依赖。
type Deps struct {
	Tenants  Tenants
	Jobs     Jobs
	Identity *identity.Service
	Accounts Accounts
	Audit    *audit.Recorder
	Logger   *slog.Logger
}

// Server 提供 /admin/* 路由。
type Server struct {
	deps Deps
	mux  *http.ServeMux
}

// New 构造管理入口。
func New(deps Deps) *Server {
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	s := &Server{deps: deps, mux: http.NewServeMux()}
	route := func(pattern, tool string, rule auth.Rule, fn handler) {
		s.mux.HandleFunc(pattern, s.wrap(tool, rule, fn))
	}
	route("GET /admin/tenants", "admin.tenants.list", auth.Platform, s.listTenants)
	route("POST /admin/tenants", "admin.tenants.upsert", auth.Platform, s.upsertTenant)
	route("GET /admin/tenants/{id}/tokens", "admin.tokens.list", auth.InPath(domain.CapProjectView), s.listTokens)
	route("POST /admin/tenants/{id}/tokens", "admin.tokens.issue", auth.InPath(domain.CapProjectWrite), s.issueToken)
	route("POST /admin/tokens/{id}/revoke", "admin.tokens.revoke", auth.OnKey(domain.CapProjectWrite), s.revokeToken)
	route("GET /admin/jobs", "admin.jobs.list", auth.Platform, s.listJobs)
	route("POST /admin/jobs/{id}/retry", "admin.jobs.retry", auth.Platform, s.retryJob)
	s.registerAccounts(route)
	s.registerInvites(route)
	// 未知路径也回 JSON 信封；Go 1.22 会优先匹配更具体的 pattern。
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		httpx.NotFoundJSON(w)
	})
	return s
}

// Handler 返回挂到 /admin/ 的处理器。
func (s *Server) Handler() http.Handler { return httpx.MethodNotAllowedJSON(s.mux) }

type handler func(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error)

func (s *Server) wrap(tool string, rule auth.Rule, fn handler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		started := time.Now()
		record := domain.AuditRecord{
			ID: domain.NewID(), CallID: domain.NewID(), TS: started, Tool: tool,
			Source:   domain.AuditSourceAdmin,
			RemoteIP: httpx.ClientIPFrom(ctx),
		}
		fail := func(err error) {
			status, code, message := mapError(err)
			record.Ms = time.Since(started).Milliseconds()
			record.Err = errorName(err)
			record.ErrMessage = message
			record.HTTPStatus = status
			s.record(ctx, record)
			httpx.Fail(w, status, code, message)
		}
		p, err := rule.Resolve(ctx, s.deps.Accounts, r)
		if err != nil {
			fail(err)
			return
		}
		record.UserID = p.Actor()
		body := map[string]any{}
		if r.Method == http.MethodPost {
			if err := decodeBody(r, &body); err != nil {
				fail(err)
				return
			}
			record.Input = redactInput(body)
		}
		tenantID, err := rule.Authorize(ctx, auth.AuthzInput{Principal: p, Request: r, Body: body, Keys: s.deps.Identity})
		record.TenantID = tenantID
		if err != nil {
			fail(err)
			return
		}
		payload, err := fn(ctx, p, r, body)
		if err != nil {
			fail(err)
			return
		}
		record.Ms = time.Since(started).Milliseconds()
		record.OK = true
		record.HTTPStatus = http.StatusOK
		s.record(ctx, record)
		httpx.OK(w, payload)
	}
}

// redactInput 去掉口令字段再写审计：口令永不进日志、审计或错误消息。
func redactInput(body map[string]any) map[string]any {
	if _, ok := body["password"]; !ok {
		return body
	}
	out := make(map[string]any, len(body))
	maps.Copy(out, body)
	out["password"] = "***"
	return out
}

// record 只记录已认证主体的调用；匿名请求不写审计，避免未登录者灌满审计表。
func (s *Server) record(ctx context.Context, record domain.AuditRecord) {
	if s.deps.Audit != nil && record.UserID != "" {
		s.deps.Audit.Record(ctx, record)
	}
}

func (s *Server) listTenants(ctx context.Context, _ domain.Principal, _ *http.Request, _ map[string]any) (any, error) {
	tenants, err := s.deps.Tenants.List(ctx)
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(tenants))
	for i := range tenants {
		records = append(records, tenantJSON(tenants[i]))
	}
	return map[string]any{"records": records, "total": len(records)}, nil
}

func (s *Server) upsertTenant(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	in := tenant.UpsertInput{
		ID:       httpx.String(body["id"]),
		UserID:   httpx.String(body["user_id"]),
		Name:     httpx.String(body["name"]),
		Disabled: httpx.Bool(body["disabled"], false),
	}
	// 未提供的 description / isMultiView 保留原值，出现（哪怕是 null）才覆盖。
	if raw, present := body["description"]; present {
		description := httpx.String(raw)
		in.Description = &description
	}
	if raw, present := body["isMultiView"]; present {
		multiView := httpx.Bool(raw, false)
		in.MultiView = &multiView
	}
	stored, err := s.deps.Tenants.Upsert(ctx, in)
	if err != nil {
		// store 把 user_id 冲突映射成 domain.ErrConflict，mapError 转成 409；
		// 原样透传，不要改写成 500。
		return nil, err
	}
	return tenantJSON(stored), nil
}

func (s *Server) listTokens(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	tenantID := r.PathValue("id")
	if _, err := s.deps.Tenants.Get(ctx, tenantID); err != nil {
		return nil, err
	}
	tokens, err := s.deps.Identity.ListTokens(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(tokens))
	for i := range tokens {
		records = append(records, tokenJSON(tokens[i]))
	}
	return map[string]any{"records": records, "total": len(records)}, nil
}

func (s *Server) issueToken(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	body map[string]any,
) (any, error) {
	tenantID := r.PathValue("id")
	raw, tok, err := s.deps.Identity.IssueToken(ctx, tenantID, httpx.String(body["label"]), p.AccountID)
	if err != nil {
		return nil, err
	}
	out := tokenJSON(tok)
	out["token"] = raw // 明文只在这里出现一次
	return out, nil
}

func (s *Server) revokeToken(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	if err := s.deps.Identity.RevokeToken(ctx, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func tenantJSON(t domain.Tenant) map[string]any {
	return map[string]any{
		"id":          t.ID,
		"user_id":     t.UserID,
		"name":        t.Name,
		"description": t.Description,
		"isMultiView": t.MultiView,
		"disabled":    t.Disabled,
		"created_at":  t.CreatedAt,
	}
}

func tokenJSON(t domain.Token) map[string]any {
	out := map[string]any{
		"id":         t.ID,
		"kind":       string(t.Kind),
		"tenant_id":  t.TenantID,
		"user_id":    t.UserID,
		"prefix":     t.Prefix,
		"label":      t.Label,
		"created_at": t.CreatedAt,
	}
	if t.LastUsedAt != nil {
		out["last_used_at"] = *t.LastUsedAt
	}
	if t.ExpiresAt != nil {
		out["expires_at"] = *t.ExpiresAt
		out["expired"] = !time.Now().Before(*t.ExpiresAt)
	} else {
		out["expired"] = false
	}
	if t.RevokedAt != nil {
		out["revoked_at"] = *t.RevokedAt
	}
	return out
}

func decodeBody(r *http.Request, dst *map[string]any) error {
	if err := httpx.RequireJSON(r); err != nil {
		return err
	}
	return httpx.DecodeJSON(r, dst)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func errorName(err error) string {
	if kind := domain.KindOf(err); kind != nil {
		return kind.Error()
	}
	return "internal"
}

func mapError(err error) (int, int, string) {
	switch {
	case errors.Is(err, httpx.ErrBodyTooLarge):
		return http.StatusRequestEntityTooLarge, httpx.CodeBadRequest, "请求体过大"
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
	case errors.Is(err, domain.ErrConflict):
		return http.StatusConflict, httpx.CodeConflict, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrRateLimited):
		return http.StatusTooManyRequests, httpx.CodeRateLimited, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrUpstreamUnavailable), errors.Is(err, domain.ErrDisabled):
		return http.StatusServiceUnavailable, httpx.CodeUnavailable, domain.SafeMessage(err)
	default:
		return http.StatusInternalServerError, httpx.CodeInternal, "内部错误"
	}
}
