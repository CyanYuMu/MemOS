package admin

import (
	"context"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// listJobs 是死信排查用的只读查询：只返回任务元数据，不返回 payload。
func (s *Server) listJobs(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	q := r.URL.Query()
	jobs, total, err := s.deps.Jobs.List(ctx, domain.JobFilter{
		Status:   domain.JobStatus(q.Get("status")),
		Kind:     domain.JobKind(q.Get("kind")),
		TenantID: q.Get("tenant_id"),
		Limit:    httpx.Int(q.Get("limit"), 50),
		Offset:   httpx.Int(q.Get("offset"), 0),
	})
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(jobs))
	for i := range jobs {
		records = append(records, jobJSON(jobs[i]))
	}
	return map[string]any{"records": records, "total": total}, nil
}

// retryJob 重放一条死信任务。「能重放」不等于「重放安全」，判定在 service/jobs；
// force 表示人已确认上游确实没写成功。
func (s *Server) retryJob(ctx context.Context, _ domain.Principal, r *http.Request, body map[string]any) (any, error) {
	force := httpx.Bool(body["force"], false) || r.URL.Query().Get("force") == "1"
	job, err := s.deps.Jobs.Retry(ctx, r.PathValue("id"), force)
	if err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "job": jobJSON(job)}, nil
}

func jobJSON(j domain.Job) map[string]any {
	out := map[string]any{
		"id":         j.ID,
		"kind":       string(j.Kind),
		"status":     string(j.Status),
		"tenant_id":  j.TenantID,
		"user_id":    j.UserID,
		"attempts":   j.Attempts,
		"last_error": j.LastError,
		"created_at": j.CreatedAt,
		"updated_at": j.UpdatedAt,
	}
	if !j.NextAttemptAt.IsZero() {
		out["next_attempt_at"] = j.NextAttemptAt
	}
	return out
}
