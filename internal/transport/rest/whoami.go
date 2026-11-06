package rest

import (
	"context"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Tenants 只用来回显项目名，避免 transport 依赖整个 tenant 用例集。
type Tenants interface {
	Get(ctx context.Context, id string) (domain.Tenant, error)
}

// handleWhoami 回显这把 token 落在哪个用户、哪个项目下，供安装脚本自检。
// 不写审计：它是安装期的连通性检查，不是一次记忆调用。
func (s *Server) handleWhoami(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		errorBody(w, http.StatusMethodNotAllowed, "只接受 GET")
		return
	}
	caller, err := s.deps.Identity.Authenticate(r.Context(), httpx.BearerToken(r.Header.Get("Authorization")))
	if err != nil {
		status, message := errorStatus(err)
		errorBody(w, status, message)
		return
	}
	project := ""
	if s.deps.Tenants != nil && caller.TenantID != "" {
		if t, err := s.deps.Tenants.Get(r.Context(), caller.TenantID); err == nil {
			project = t.Name
		}
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{
		"status":       "success",
		"user_id":      caller.UserID,
		"project_id":   caller.TenantID,
		"project_name": project,
	})
}
