package localsync

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	svc "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/localsync"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const pollPendingCode = 20200

func (s *Server) createDeviceCode(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	if err := httpx.RequireJSON(r); err != nil {
		s.fail(w, r, err)
		return
	}
	var body struct {
		DeviceLabel string `json:"deviceLabel"`
		AgentKind   string `json:"agentKind"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		s.fail(w, r, err)
		return
	}
	grant, err := s.deps.Service.CreateDeviceCode(r.Context(), httpx.ClientIPFrom(r.Context()), body.DeviceLabel, body.AgentKind)
	if err != nil {
		s.fail(w, r, err)
		return
	}
	httpx.OK(w, map[string]any{
		"deviceCode":      grant.DeviceCode,
		"userCode":        grant.UserCode,
		"verificationUri": s.verificationURI(r, grant.UserCode),
		"expiresIn":       grant.ExpiresIn,
		"interval":        grant.Interval,
	})
}

func (s *Server) pollDeviceToken(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	if err := httpx.RequireJSON(r); err != nil {
		s.fail(w, r, err)
		return
	}
	var body struct {
		DeviceCode string `json:"deviceCode"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		s.fail(w, r, err)
		return
	}
	grant, err := s.deps.Service.PollDeviceToken(r.Context(), body.DeviceCode)
	switch {
	case errors.Is(err, svc.ErrAuthorizationPending):
		httpx.WriteJSON(w, http.StatusAccepted, httpx.Envelope{
			Code: pollPendingCode, Data: map[string]any{"status": "authorization_pending"}, Message: "authorization_pending",
		})
	case errors.Is(err, svc.ErrAccessDenied):
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "用户已拒绝授权或授权码已被使用")
	case errors.Is(err, svc.ErrExpiredToken):
		httpx.Fail(w, http.StatusBadRequest, httpx.CodeBadRequest, "授权码已过期，请重新发起")
	case err != nil:
		s.fail(w, r, err)
	default:
		httpx.OK(w, map[string]any{
			"deviceToken": grant.DeviceToken,
			"deviceId":    grant.DeviceID,
			"tenantId":    grant.TenantID,
			"projectName": grant.ProjectName,
		})
	}
}

func (s *Server) state(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	identity, ok := s.device(w, r)
	if !ok {
		return
	}
	state, err := s.deps.Service.State(r.Context(), identity)
	if err != nil {
		s.fail(w, r, err)
		return
	}
	httpx.OK(w, map[string]any{
		"deviceId":       state.DeviceID,
		"tenantId":       state.TenantID,
		"lastBatchId":    state.LastBatchID,
		"lastAcceptedAt": formatTimePtr(state.LastAcceptedAt),
		"quota": map[string]any{
			"usedBytes":    state.Quota.UsedBytes,
			"limitBytes":   state.Quota.LimitBytes,
			"usedRows":     state.Quota.UsedRows,
			"limitRows":    state.Quota.LimitRows,
			"pendingBytes": state.Quota.PendingBytes,
			"pendingRows":  state.Quota.PendingRows,
		},
	})
}

func (s *Server) createBatch(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	identity, ok := s.device(w, r)
	if !ok {
		return
	}
	if err := httpx.RequireJSON(r); err != nil {
		s.fail(w, r, err)
		return
	}
	var body struct {
		BatchID   string         `json:"batchId"`
		Layers    []string       `json:"layers"`
		Manifest  map[string]int `json:"manifest"`
		TraceMode string         `json:"traceMode"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		s.fail(w, r, err)
		return
	}
	grant, err := s.deps.Service.CreateBatch(r.Context(), identity, svc.CreateBatchInput{
		BatchID:   body.BatchID,
		Layers:    body.Layers,
		Manifest:  body.Manifest,
		TraceMode: body.TraceMode,
	})
	if err != nil {
		s.fail(w, r, err)
		return
	}
	httpx.OK(w, map[string]any{
		"batchId":   grant.BatchID,
		"expiresAt": grant.ExpiresAt.UTC().Format(time.RFC3339),
	})
}

func (s *Server) putChunk(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	identity, ok := s.device(w, r)
	if !ok {
		return
	}
	seq, err := strconv.Atoi(r.PathValue("seq"))
	if err != nil {
		s.fail(w, r, domain.Invalid("local_sync", "seq 必须是整数"))
		return
	}
	limit := s.deps.Service.Options().MaxChunkBytes
	body, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	if err != nil {
		if httpx.IsBodyTooLarge(err) {
			err = httpx.ErrBodyTooLarge
		}
		s.fail(w, r, err)
		return
	}
	if int64(len(body)) > limit {
		s.fail(w, r, domain.Wrap(domain.ErrQuotaExceeded, "local_sync", "单块超过上限", nil))
		return
	}
	received, err := s.deps.Service.PutChunk(r.Context(), identity, r.PathValue("batchId"), seq, body)
	if err != nil {
		s.fail(w, r, err)
		return
	}
	httpx.OK(w, map[string]any{"received": received})
}

func (s *Server) commitBatch(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	identity, ok := s.device(w, r)
	if !ok {
		return
	}
	result, err := s.deps.Service.CommitBatch(r.Context(), identity, r.PathValue("batchId"))
	if err != nil {
		s.fail(w, r, err)
		return
	}
	httpx.OK(w, commitJSON(result))
}

func (s *Server) manual(w http.ResponseWriter, r *http.Request) {
	if !s.ensureEnabled(w) {
		return
	}
	p, ok := s.session(w, r)
	if !ok {
		return
	}
	if err := httpx.RequireJSON(r); err != nil {
		s.fail(w, r, err)
		return
	}
	var body struct {
		ProjectID string            `json:"projectId"`
		BatchID   string            `json:"batchId"`
		Layers    []string          `json:"layers"`
		Manifest  map[string]int    `json:"manifest"`
		TraceMode string            `json:"traceMode"`
		Rows      []json.RawMessage `json:"rows"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		s.fail(w, r, err)
		return
	}
	result, err := s.deps.Service.Manual(r.Context(), p, body.ProjectID, svc.ManualBundle{
		BatchID:   body.BatchID,
		Layers:    body.Layers,
		Manifest:  body.Manifest,
		TraceMode: body.TraceMode,
		Rows:      body.Rows,
	})
	if err != nil {
		s.fail(w, r, err)
		return
	}
	httpx.OK(w, commitJSON(result))
}

func commitJSON(result domain.LocalCommitResult) map[string]any {
	cursor := ""
	if !result.Cursor.IsZero() {
		cursor = result.Cursor.UTC().Format(time.RFC3339Nano)
	}
	reasons := result.RejectedReasons
	if reasons == nil {
		reasons = []domain.LocalRejectedReason{}
	}
	return map[string]any{
		"status":          result.Status,
		"accepted":        result.Accepted,
		"rejected":        result.Rejected,
		"rejectedReasons": reasons,
		"cursor":          cursor,
	}
}

func formatTimePtr(t *time.Time) string {
	if t == nil || t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339Nano)
}
