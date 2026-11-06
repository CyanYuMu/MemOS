package auth

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func (s *Server) onboarding(w http.ResponseWriter, r *http.Request) {
	p, err := s.deps.Sessions.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		s.onboardingError(w, r, err)
		return
	}
	if s.deps.Onboarding == nil {
		httpx.Fail(w, http.StatusServiceUnavailable, httpx.CodeInternal, "指引暂不可用，请稍后重试")
		return
	}
	project := r.URL.Query().Get("projectId")
	var result domain.OnboardingProgress
	if r.Method == http.MethodGet {
		result, err = s.deps.Onboarding.Get(r.Context(), p, project)
	} else {
		result, err = s.updateOnboarding(r, p)
	}
	if err != nil {
		s.onboardingError(w, r, err)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	httpx.OK(w, result)
}

func (s *Server) updateOnboarding(r *http.Request, p domain.Principal) (domain.OnboardingProgress, error) {
	var in struct {
		ProjectID  string                       `json:"projectId"`
		Action     string                       `json:"action"`
		Revision   *int64                       `json:"revision"`
		Checkpoint *domain.OnboardingCheckpoint `json:"checkpoint"`
	}
	if err := httpx.RequireJSON(r); err != nil {
		return domain.OnboardingProgress{}, domain.Invalid("onboarding", "请求格式不正确")
	}
	if err := httpx.DecodeJSON(r, &in); err != nil || in.Revision == nil || *in.Revision < 0 {
		return domain.OnboardingProgress{}, domain.Invalid("onboarding", "请求格式不正确")
	}
	var checkpoints []domain.OnboardingCheckpoint
	if in.Checkpoint != nil {
		checkpoints = append(checkpoints, *in.Checkpoint)
	}
	return s.deps.Onboarding.Update(r.Context(), p, in.ProjectID, in.Action, *in.Revision, checkpoints...)
}

func (s *Server) onboardingError(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case errors.Is(err, domain.ErrUnauthorized):
		httpx.Fail(w, http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err))
	case errors.Is(err, domain.ErrForbidden):
		httpx.Fail(w, http.StatusForbidden, httpx.CodeForbidden, domain.SafeMessage(err))
	case errors.Is(err, domain.ErrInvalidInput):
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, domain.SafeMessage(err))
	case errors.Is(err, domain.ErrConflict):
		httpx.Fail(w, http.StatusConflict, httpx.CodeConflict, domain.SafeMessage(err))
	default:
		s.deps.Logger.ErrorContext(r.Context(), "onboarding failed", slog.Any("err", err))
		httpx.Fail(w, http.StatusInternalServerError, httpx.CodeInternal, "读取进度失败，请稍后重试")
	}
}
