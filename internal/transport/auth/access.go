// Package auth 是控制台的登录入口与各入口共用的授权规则。
package auth

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Authenticator 把请求凭据解析成主体，由 account 服务实现。
type Authenticator interface {
	Authenticate(ctx context.Context, c domain.Credentials) (domain.Principal, error)
}

// KeyLookup 按 Key ID 取其元数据，用来先确定 Key 所属项目再判权。
type KeyLookup interface {
	GetToken(ctx context.Context, id string) (domain.Token, error)
}

// Scope 决定一条路由的项目从请求的哪里取。
type Scope int

const (
	ScopeAuthed       Scope = iota // 登录即可
	ScopePublic                    // 可匿名；已登录时仍解析主体
	ScopePlatform                  // 平台管理员
	ScopeBodyProject               // 请求体或查询串里的 projectId，缺省取会话的当前项目
	ScopeQueryTenant               // 查询串里的 tenant
	ScopeQueryProject              // 查询串里的 projectId
	ScopePathProject               // 路径参数 {id} 是项目
	ScopeKey                       // 路径参数 {id} 是 Key，按 Key 所属项目判定
)

// Rule 是一条路由的授权要求：项目从哪里取，以及需要什么能力。
type Rule struct {
	Scope Scope
	Cap   domain.Capability
	// Strict 只对 ScopeBodyProject 生效：要求请求显式给出 projectId（body 或 query），
	// 不回落到会话的当前项目。变更类路由用它，避免漏传字段时写到错误项目。
	Strict bool
}

var (
	Authed   = Rule{Scope: ScopeAuthed}
	Public   = Rule{Scope: ScopePublic}
	Platform = Rule{Scope: ScopePlatform, Cap: domain.CapPlatformManage}
)

// AuthedWith 要求登录并具备与项目无关的能力（如 project:create）。
func AuthedWith(c domain.Capability) Rule { return Rule{Scope: ScopeAuthed, Cap: c} }

// InBody 要求主体在 projectId 指向的项目里具备 capability。projectId 先取请求体，
// 再取查询串，都没有时取会话的当前项目（前端的 memory/detail、memory/delete 就不带 projectId）。
func InBody(c domain.Capability) Rule { return Rule{Scope: ScopeBodyProject, Cap: c} }

// InBodyStrict 与 InBody 相同，但要求请求显式给出 projectId；变更类路由用它，
// 防止字段漏传或拼错时静默落到会话当前项目。
func InBodyStrict(c domain.Capability) Rule {
	return Rule{Scope: ScopeBodyProject, Cap: c, Strict: true}
}

// InQuery 要求主体在查询串 tenant 指向的项目里具备 capability。
func InQuery(c domain.Capability) Rule { return Rule{Scope: ScopeQueryTenant, Cap: c} }

// InQueryProject 要求主体在查询串 projectId 指向的项目里具备 capability。
// 本地同步的 Dashboard 路由按前端约定用 projectId 而不是 tenant。
func InQueryProject(c domain.Capability) Rule { return Rule{Scope: ScopeQueryProject, Cap: c} }

// InPath 要求主体在路径参数 {id} 指向的项目里具备 capability。
func InPath(c domain.Capability) Rule { return Rule{Scope: ScopePathProject, Cap: c} }

// OnKey 要求主体在路径参数 {id} 这把 Key 所属的项目里具备 capability。
func OnKey(c domain.Capability) Rule { return Rule{Scope: ScopeKey, Cap: c} }

// Resolve 解析主体。Public 路由遇到未登录返回零值主体而不是错误。
func (rl Rule) Resolve(ctx context.Context, a Authenticator, r *http.Request) (domain.Principal, error) {
	p, err := a.Authenticate(ctx, httpx.CredentialsOf(r))
	if err != nil && rl.Scope == ScopePublic && errors.Is(err, domain.ErrUnauthorized) {
		return domain.Principal{}, nil
	}
	return p, err
}

// AuthzInput 是一次判权的入参。
type AuthzInput struct {
	Principal domain.Principal
	Request   *http.Request
	Body      map[string]any
	Keys      KeyLookup
}

// Authorize 按规则判权，返回被判权的项目 ID（没有项目维度时为空串）。
// 非成员一律得到 ErrForbidden，不区分“不存在”和“无权限”。
func (rl Rule) Authorize(ctx context.Context, in AuthzInput) (string, error) {
	p, r, body, keys := in.Principal, in.Request, in.Body, in.Keys
	switch rl.Scope {
	case ScopeAuthed, ScopePublic:
		if rl.Cap == "" {
			return "", nil
		}
		return "", p.RequireCap("", rl.Cap)
	case ScopePlatform:
		return "", p.RequireCap("", domain.CapPlatformManage)
	case ScopeBodyProject:
		explicit := explicitProjectID(r, body)
		if rl.Strict && explicit == "" {
			return "", domain.Invalid("acl", "缺少 projectId")
		}
		if explicit == "" {
			explicit = p.CurrentTenant
		}
		return rl.inProject(p, explicit, "缺少 projectId，且当前没有可用项目")
	case ScopeQueryTenant:
		return rl.inProject(p, strings.TrimSpace(r.URL.Query().Get("tenant")), "缺少 tenant")
	case ScopeQueryProject:
		return rl.inProject(p, strings.TrimSpace(r.URL.Query().Get("projectId")), "缺少 projectId")
	case ScopePathProject:
		return rl.inProject(p, r.PathValue("id"), "缺少项目 ID")
	case ScopeKey:
		return rl.onKey(ctx, p, r.PathValue("id"), keys)
	default:
		return "", domain.Wrap(domain.ErrForbidden, "acl", "未知的授权规则", nil)
	}
}

func (rl Rule) inProject(p domain.Principal, tenantID, missing string) (string, error) {
	if tenantID == "" {
		return "", domain.Invalid("acl", missing)
	}
	return tenantID, p.RequireCap(tenantID, rl.Cap)
}

func (rl Rule) onKey(ctx context.Context, p domain.Principal, keyID string, keys KeyLookup) (string, error) {
	tok, err := keys.GetToken(ctx, keyID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) && !p.PlatformAdmin {
			return "", domain.Wrap(domain.ErrForbidden, "acl", "没有该项目的操作权限", nil)
		}
		return "", err
	}
	if tok.TenantID == "" {
		return "", p.RequireCap("", domain.CapPlatformManage)
	}
	return tok.TenantID, p.RequireCap(tok.TenantID, rl.Cap)
}

// explicitProjectID 只看请求本身，不回落到会话当前项目。
func explicitProjectID(r *http.Request, body map[string]any) string {
	if id := stringOf(body["projectId"]); id != "" {
		return id
	}
	return strings.TrimSpace(r.URL.Query().Get("projectId"))
}

func stringOf(value any) string {
	text, _ := value.(string)
	return strings.TrimSpace(text)
}
