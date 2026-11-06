package auth

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// 账号自服务：改口令、看自己的会话、踢掉其他会话。只接受账号会话，
// 管理 Token 没有会话可直接吊销，因而不支持这三条。

type passwordInput struct {
	OldPassword string `json:"oldPassword"`
	NewPassword string `json:"newPassword"`
}

func (s *Server) changePassword(w http.ResponseWriter, r *http.Request) {
	p, ok := s.sessionPrincipal(w, r)
	if !ok {
		return
	}
	var in passwordInput
	if err := httpx.RequireJSON(r); err != nil {
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "请求必须是 application/json")
		return
	}
	if err := httpx.DecodeJSON(r, &in); err != nil {
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "请求格式不正确")
		return
	}
	err := s.deps.Sessions.ChangePassword(r.Context(), account.PasswordChangeInput{
		AccountID:   p.AccountID,
		SessionID:   p.SessionID,
		OldPassword: in.OldPassword,
		NewPassword: in.NewPassword,
		RemoteIP:    remoteIP(r),
	})
	s.audit(r.Context(), authAudit{Tool: "auth.password", Username: p.Username, IP: remoteIP(r), Err: err})
	switch {
	case err == nil:
		w.Header().Set("Cache-Control", "no-store")
		httpx.OK(w, map[string]any{"ok": true})
	case errors.Is(err, domain.ErrUnauthorized):
		httpx.Fail(w, http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err))
	case errors.Is(err, domain.ErrInvalidInput):
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, domain.SafeMessage(err))
	case errors.Is(err, domain.ErrRateLimited):
		httpx.SetRetryAfter(w, s.deps.LoginFailWindow)
		httpx.Fail(w, http.StatusTooManyRequests, httpx.CodeRateLimited, domain.SafeMessage(err))
	default:
		s.deps.Logger.ErrorContext(r.Context(), "change password failed", slog.Any("err", err))
		httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "内部错误")
	}
}

func (s *Server) listSessions(w http.ResponseWriter, r *http.Request) {
	p, ok := s.sessionPrincipal(w, r)
	if !ok {
		return
	}
	sessions, err := s.deps.Sessions.Sessions(r.Context(), p.AccountID)
	if err != nil {
		s.deps.Logger.ErrorContext(r.Context(), "list sessions failed", slog.Any("err", err))
		httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "内部错误")
		return
	}
	rows := make([]map[string]any, 0, len(sessions))
	for i := range sessions {
		sess := &sessions[i]
		row := map[string]any{
			"id":         sess.ID,
			"current":    sess.ID == p.SessionID,
			"createdAt":  domain.FormatBeijing(sess.CreatedAt),
			"expiresAt":  domain.FormatBeijing(sess.ExpiresAt),
			"projectId":  sess.CurrentTenantID,
			"lastSeenAt": "",
		}
		if sess.LastSeenAt != nil {
			row["lastSeenAt"] = domain.FormatBeijing(*sess.LastSeenAt)
		}
		rows = append(rows, row)
	}
	w.Header().Set("Cache-Control", "no-store")
	httpx.OK(w, map[string]any{"sessions": rows, "total": len(rows)})
}

func (s *Server) revokeOtherSessions(w http.ResponseWriter, r *http.Request) {
	p, ok := s.sessionPrincipal(w, r)
	if !ok {
		return
	}
	if err := httpx.RequireJSON(r); err != nil {
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "请求必须是 application/json")
		return
	}
	n, err := s.deps.Sessions.RevokeOtherSessions(r.Context(), p.AccountID, p.SessionID)
	s.audit(r.Context(), authAudit{Tool: "auth.sessions.revoke", Username: p.Username, IP: remoteIP(r), Err: err})
	if err != nil {
		s.deps.Logger.ErrorContext(r.Context(), "revoke sessions failed", slog.Any("err", err))
		httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "内部错误")
		return
	}
	httpx.OK(w, map[string]any{"revoked": n})
}

// sessionPrincipal 只接受账号会话；管理 Token 返回 400。
func (s *Server) sessionPrincipal(w http.ResponseWriter, r *http.Request) (domain.Principal, bool) {
	p, err := s.deps.Sessions.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		switch {
		case errors.Is(err, domain.ErrUnauthorized):
			httpx.Fail(w, http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err))
		case errors.Is(err, domain.ErrForbidden):
			httpx.Fail(w, http.StatusForbidden, httpx.CodeForbidden, domain.SafeMessage(err))
		default:
			s.deps.Logger.ErrorContext(r.Context(), "auth principal failed", slog.Any("err", err))
			httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "内部错误")
		}
		return domain.Principal{}, false
	}
	if p.Kind != domain.PrincipalAccount || p.SessionID == "" {
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "该操作需要控制台账号登录，管理 Token 或统一认证不支持")
		return domain.Principal{}, false
	}
	return p, true
}
