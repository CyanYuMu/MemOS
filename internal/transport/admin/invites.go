package admin

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
)

func (s *Server) registerInvites(route func(pattern, tool string, rule auth.Rule, fn handler)) {
	route("GET /admin/tenants/{id}/member-candidates", "admin.members.candidates", auth.InPath(domain.CapProjectManage), s.memberCandidates)
	route("GET /admin/tenants/{id}/invites", "admin.invites.list", auth.InPath(domain.CapProjectManage), s.listInvites)
	route("POST /admin/tenants/{id}/invites", "admin.invites.create", auth.InPath(domain.CapProjectManage), s.createInvite)
	route("POST /admin/tenants/{id}/invites/{inviteId}/revoke", "admin.invites.revoke", auth.InPath(domain.CapProjectManage), s.revokeInvite)
	route("POST /admin/tenants/{id}/invites/{inviteId}/role", "admin.invites.role", auth.InPath(domain.CapProjectManage), s.updateInviteRole)
}

func (s *Server) memberCandidates(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	candidates, err := s.deps.Accounts.MemberCandidates(ctx, p, r.PathValue("id"), r.URL.Query().Get("query"))
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(candidates))
	for _, c := range candidates {
		records = append(records, map[string]any{
			"accountId":   c.AccountID,
			"username":    c.Username,
			"displayName": c.DisplayName,
			"disabled":    c.Disabled,
			"member":      c.Member,
		})
	}
	return map[string]any{"records": records, "total": len(records)}, nil
}

func (s *Server) listInvites(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	invites, err := s.deps.Accounts.ListInvites(ctx, p, r.PathValue("id"))
	if err != nil {
		return nil, err
	}
	now := time.Now()
	records := make([]map[string]any, 0, len(invites))
	for i := range invites {
		records = append(records, inviteJSON(invites[i], now))
	}
	return map[string]any{"records": records, "total": len(records)}, nil
}

func (s *Server) createInvite(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	body map[string]any,
) (any, error) {
	expiresAt, err := inviteExpiry(body)
	if err != nil {
		return nil, err
	}
	created, err := s.deps.Accounts.CreateInvite(ctx, account.CreateInviteInput{
		Actor:     p,
		TenantID:  r.PathValue("id"),
		Role:      domain.Role(strings.TrimSpace(stringOf(body["role"]))),
		ExpiresAt: expiresAt,
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"id":        created.Invite.ID,
		"tenantId":  created.Invite.TenantID,
		"role":      string(created.Invite.Role),
		"path":      created.Path,
		"createdAt": created.Invite.CreatedAt,
		"expiresAt": created.Invite.ExpiresAt,
	}, nil
}

func (s *Server) revokeInvite(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	if err := s.deps.Accounts.RevokeInvite(ctx, p, r.PathValue("id"), r.PathValue("inviteId")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

// updateInviteRole 修改未使用邀请的角色；已使用邀请请到成员管理里改角色。
func (s *Server) updateInviteRole(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	body map[string]any,
) (any, error) {
	role := domain.Role(strings.TrimSpace(stringOf(body["role"])))
	if err := s.deps.Accounts.UpdateInviteRole(ctx, account.InviteRoleInput{
		Actor:    p,
		TenantID: r.PathValue("id"),
		InviteID: r.PathValue("inviteId"),
		Role:     role,
	}); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "role": string(role)}, nil
}

// inviteExpiry 解析 expiresAt（RFC3339）或 expiresIn（Go duration）；同时给以 expiresAt 为准。
func inviteExpiry(body map[string]any) (time.Time, error) {
	if raw := strings.TrimSpace(stringOf(body["expiresAt"])); raw != "" {
		at, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			return time.Time{}, domain.Invalid("admin", "expiresAt 必须是 RFC3339 时间")
		}
		return at, nil
	}
	if raw := strings.TrimSpace(stringOf(body["expiresIn"])); raw != "" {
		d, err := time.ParseDuration(raw)
		if err != nil || d <= 0 {
			return time.Time{}, domain.Invalid("admin", "expiresIn 必须是正的时间间隔，如 24h")
		}
		return time.Now().Add(d), nil
	}
	return time.Time{}, nil
}

func inviteJSON(inv domain.ProjectInvite, now time.Time) map[string]any {
	out := map[string]any{
		"id":        inv.ID,
		"tenantId":  inv.TenantID,
		"role":      string(inv.Role),
		"status":    string(inv.Status(now)),
		"createdAt": inv.CreatedAt,
		"expiresAt": inv.ExpiresAt,
	}
	if inv.UsedAt != nil {
		out["usedAt"] = *inv.UsedAt
	}
	if inv.UsedByAccountID != "" {
		out["usedByAccountId"] = inv.UsedByAccountID
	}
	if inv.RevokedAt != nil {
		out["revokedAt"] = *inv.RevokedAt
	}
	return out
}

func stringOf(v any) string {
	text, _ := v.(string)
	return text
}
