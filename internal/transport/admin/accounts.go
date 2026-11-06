package admin

import (
	"context"
	"net/http"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func (s *Server) registerAccounts(route func(pattern, tool string, rule auth.Rule, fn handler)) {
	route("GET /admin/accounts", "admin.accounts.list", auth.Platform, s.listAccounts)
	route("GET /admin/accounts/{id}", "admin.accounts.get", auth.Platform, s.getAccount)
	route("POST /admin/accounts", "admin.accounts.create", auth.Platform, s.createAccount)
	route("POST /admin/accounts/{id}", "admin.accounts.update", auth.Platform, s.updateAccount)
	route("POST /admin/accounts/{id}/password", "admin.accounts.password", auth.Platform, s.resetPassword)
	route("DELETE /admin/accounts/{id}", "admin.accounts.delete", auth.Platform, s.deleteAccount)
	route("POST /admin/accounts/{id}/sessions/revoke", "admin.accounts.sessions_revoke", auth.Platform, s.revokeSessions)
	route("GET /admin/tenants/{id}/members", "admin.members.list", auth.InPath(domain.CapProjectManage), s.listMembers)
	route("POST /admin/tenants/{id}/members", "admin.members.set", auth.InPath(domain.CapProjectManage), s.setMember)
	// 退出项目只需要“是成员”，不要求管理权限；目标账号固定为当前主体。
	route("POST /admin/tenants/{id}/members/leave", "admin.members.leave", auth.InPath(domain.CapProjectView), s.leaveProject)
	route("POST /admin/tenants/{id}/members/{accountId}/remove", "admin.members.remove", auth.InPath(domain.CapProjectManage), s.removeMember)
}

func (s *Server) listAccounts(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	q := r.URL.Query()
	accounts, total, err := s.deps.Accounts.ListAccountsPage(ctx, q.Get("query"),
		httpx.Int(q.Get("page"), 1), httpx.Int(q.Get("pageSize"), 20))
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(accounts))
	for i := range accounts {
		records = append(records, accountJSON(accounts[i]))
	}
	return map[string]any{"records": records, "total": total}, nil
}

func (s *Server) getAccount(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	acct, err := s.deps.Accounts.GetAccount(ctx, r.PathValue("id"))
	if err != nil {
		return nil, err
	}
	return accountJSON(acct), nil
}

func (s *Server) createAccount(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	acct, err := s.deps.Accounts.CreateAccount(ctx, account.CreateAccountInput{
		Username:      httpx.String(body["username"]),
		Password:      rawString(body["password"]),
		DisplayName:   httpx.String(body["display_name"]),
		PlatformAdmin: httpx.Bool(body["platform_admin"], false),
	})
	if err != nil {
		return nil, err
	}
	return accountJSON(acct), nil
}

func (s *Server) updateAccount(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	body map[string]any,
) (any, error) {
	in := account.UpdateAccountInput{}
	if raw, present := body["display_name"]; present {
		text, ok := raw.(string)
		if !ok {
			return nil, domain.Invalid("admin", "display_name 必须是字符串")
		}
		name := strings.TrimSpace(text)
		in.DisplayName = &name
	}
	// 布尔字段只接受 JSON bool。以前类型不对会被静默忽略，调用方以为改成功了。
	if raw, present := body["disabled"]; present {
		flag, ok := raw.(bool)
		if !ok {
			return nil, domain.Invalid("admin", "disabled 必须是布尔值")
		}
		in.Disabled = &flag
	}
	if raw, present := body["platform_admin"]; present {
		flag, ok := raw.(bool)
		if !ok {
			return nil, domain.Invalid("admin", "platform_admin 必须是布尔值")
		}
		in.PlatformAdmin = &flag
	}
	acct, err := s.deps.Accounts.UpdateAccount(ctx, p.AccountID, r.PathValue("id"), in)
	if err != nil {
		return nil, err
	}
	return accountJSON(acct), nil
}

func (s *Server) resetPassword(
	ctx context.Context,
	_ domain.Principal,
	r *http.Request,
	body map[string]any,
) (any, error) {
	if err := s.deps.Accounts.ResetPassword(ctx, r.PathValue("id"), rawString(body["password"])); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

// deleteAccount 删除账号。不能删自己、不能删最后一个启用的平台管理员（服务层判定）；
// 审计里的账号记录保留，删除动作本身也会写一条 admin.accounts.delete。
func (s *Server) deleteAccount(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	if err := s.deps.Accounts.DeleteAccount(ctx, p.AccountID, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func (s *Server) revokeSessions(
	ctx context.Context,
	_ domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	if err := s.deps.Accounts.RevokeSessions(ctx, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func (s *Server) listMembers(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	tenantID := r.PathValue("id")
	if _, err := s.deps.Tenants.Get(ctx, tenantID); err != nil {
		return nil, err
	}
	members, err := s.deps.Accounts.ListMembers(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	accounts, err := s.deps.Accounts.ListAccounts(ctx)
	if err != nil {
		return nil, err
	}
	byID := make(map[string]domain.Account, len(accounts))
	for i := range accounts {
		byID[accounts[i].ID] = accounts[i]
	}
	records := make([]map[string]any, 0, len(members))
	for _, m := range members {
		acct := byID[m.AccountID]
		records = append(records, map[string]any{
			"account_id":   m.AccountID,
			"username":     acct.Username,
			"display_name": acct.DisplayName,
			"disabled":     acct.Disabled,
			"role":         string(m.Role),
			"created_at":   m.CreatedAt,
		})
	}
	return map[string]any{"records": records, "total": len(records)}, nil
}

func (s *Server) setMember(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error) {
	ref := firstNonEmpty(httpx.String(body["account_id"]), httpx.String(body["username"]))
	if ref == "" {
		return nil, domain.Invalid("admin", "account_id 与 username 至少给一个")
	}
	acct, err := s.deps.Accounts.ResolveAccount(ctx, ref)
	if err != nil {
		return nil, err
	}
	tenantID := r.PathValue("id")
	role := domain.Role(httpx.String(body["role"]))
	if err := s.deps.Accounts.SetMember(ctx, account.MemberInput{
		ActorAccountID: p.AccountID,
		TenantID:       tenantID,
		AccountID:      acct.ID,
		Role:           role,
	}); err != nil {
		return nil, err
	}
	return map[string]any{"account_id": acct.ID, "tenant_id": tenantID, "role": string(role)}, nil
}

func (s *Server) removeMember(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	if err := s.deps.Accounts.RemoveMember(ctx, p.AccountID, r.PathValue("id"), r.PathValue("accountId")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

// leaveProject 是成员的自助退出入口。管理 Token / 统一认证没有控制台账号，
// 不能代表某个人退出；最后一个 owner 由服务层拒绝。
func (s *Server) leaveProject(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	if p.Kind != domain.PrincipalAccount || p.AccountID == "" {
		return nil, domain.Wrap(domain.ErrForbidden, "admin", "该操作需要控制台账号登录", nil)
	}
	if err := s.deps.Accounts.LeaveProject(ctx, p.AccountID, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func accountJSON(a domain.Account) map[string]any {
	out := map[string]any{
		"id":             a.ID,
		"username":       a.Username,
		"display_name":   a.DisplayName,
		"email":          a.Email,
		"platform_admin": a.PlatformAdmin,
		"disabled":       a.Disabled,
		"created_at":     a.CreatedAt,
	}
	if a.LastLoginAt != nil {
		out["last_login_at"] = *a.LastLoginAt
	}
	return out
}

// rawString 原样取字符串，口令两端的空白也是口令的一部分。
func rawString(value any) string {
	text, _ := value.(string)
	return text
}
