package localsync

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const maxPromoteIDs = 200

// promoteJob 是 jobs 表里 local_promote 的负载。只含转存所需的最小字段，
// 不含设备令牌、审计正文或任何凭据。
type promoteJob struct {
	LocalMemoryID string   `json:"localMemoryId"`
	TenantID      string   `json:"tenantId"`
	UserID        string   `json:"userId"`
	JobID         string   `json:"jobId"`
	Layer         string   `json:"layer"`
	Title         string   `json:"title"`
	Content       string   `json:"content"`
	Tags          []string `json:"tags"`
}

// Promote 把一页里选中的镜像逐条入队转存。已入队/已成功的行计入 queued（幂等）。
func (s *Service) Promote(ctx context.Context, p domain.Principal, tenantID string, ids []string) (domain.LocalPromoteResult, error) {
	tenantID = trimmed(tenantID)
	if tenantID == "" {
		return domain.LocalPromoteResult{}, domain.Invalid("local_sync", "缺少 projectId")
	}
	if err := p.RequireCap(tenantID, domain.CapProjectWrite); err != nil {
		return domain.LocalPromoteResult{}, err
	}
	unique := uniqueIDs(ids)
	if len(unique) == 0 {
		return domain.LocalPromoteResult{}, domain.Invalid("local_sync", "ids 至少需要一条")
	}
	if len(unique) > maxPromoteIDs {
		return domain.LocalPromoteResult{}, domain.Invalid("local_sync", "单次最多转存 200 条")
	}
	tenant, err := s.store.GetTenant(ctx, tenantID)
	if err != nil {
		return domain.LocalPromoteResult{}, err
	}
	if tenant.UserID == "" {
		return domain.LocalPromoteResult{}, domain.Wrap(domain.ErrDisabled, "local_sync",
			"项目未绑定上游 user_id，无法转存", nil)
	}

	result := domain.LocalPromoteResult{Queued: make([]string, 0, len(unique)), Failed: make([]domain.LocalPromoteFailure, 0)}
	for _, id := range unique {
		queued := s.promoteOne(ctx, tenant, id, &result)
		if queued {
			result.Queued = append(result.Queued, id)
		}
	}
	s.record(ctx, auditRecord("local_sync.memory.promote", tenantID, p.Username, map[string]any{
		"memory_ids": result.Queued,
		"failed":     len(result.Failed),
	}, nil))
	return result, nil
}

// promoteOne 处理单条；返回 true 表示已进入队列或已处于终态成功（幂等）。
func (s *Service) promoteOne(ctx context.Context, tenant domain.Tenant, id string, result *domain.LocalPromoteResult) bool {
	memory, err := s.store.GetMemory(ctx, tenant.ID, id)
	if err != nil {
		result.Failed = append(result.Failed, failure(id, err))
		return false
	}
	switch memory.PromoteStatus {
	case domain.LocalPromoteQueued, domain.LocalPromoteRunning, domain.LocalPromoteSucceeded:
		return true
	}
	payload, err := decompressPayload(memory.Payload)
	if err != nil {
		result.Failed = append(result.Failed, failure(id, err))
		return false
	}
	content := promoteContent(memory, payload)
	if content == "" {
		result.Failed = append(result.Failed, domain.LocalPromoteFailure{ID: id, Message: "没有可转存的内容"})
		return false
	}
	jobID := domain.NewID()
	if err := s.store.MarkPromoteQueued(ctx, tenant.ID, id, jobID, s.now()); err != nil {
		if errors.Is(err, domain.ErrConflict) {
			// 另一个请求已经入队；幂等返回成功。
			return true
		}
		result.Failed = append(result.Failed, failure(id, err))
		return false
	}
	body, err := json.Marshal(promoteJob{
		LocalMemoryID: id,
		TenantID:      tenant.ID,
		UserID:        tenant.UserID,
		JobID:         jobID,
		Layer:         string(memory.Layer),
		Title:         memory.Title,
		Content:       content,
		Tags:          promoteTags(payload, memory.Layer),
	})
	if err != nil {
		s.failPromote(ctx, tenant.ID, id, jobID, err)
		result.Failed = append(result.Failed, failure(id, err))
		return false
	}
	job := domain.Job{
		ID:       jobID,
		Kind:     domain.JobKindLocalPromote,
		Status:   domain.JobStatusPending,
		TenantID: tenant.ID,
		UserID:   tenant.UserID,
		Payload:  body,
	}
	if err := s.store.EnqueueJob(ctx, job); err != nil {
		s.failPromote(ctx, tenant.ID, id, jobID, err)
		result.Failed = append(result.Failed, failure(id, err))
		return false
	}
	return true
}

// failPromote 在入队失败时把行恢复到可重试状态。
func (s *Service) failPromote(ctx context.Context, tenantID, id, jobID string, cause error) {
	if err := s.store.MarkPromoteResult(ctx, tenantID, id, domain.LocalPromoteFailed, jobID, safeText(cause), s.now()); err != nil {
		s.log.WarnContext(ctx, "reset promote state failed",
			slog.String("memory_id", id), slog.String("error", err.Error()))
	}
}

// RunPromoteJob 由 worker 调用：同步写上游 add_message，抽取由上游继续。
func (s *Service) RunPromoteJob(ctx context.Context, payload []byte) error {
	job, err := decodePromoteJob(payload)
	if err != nil {
		return domain.Invalid("local_promote", "任务负载无法解析")
	}
	if job.TenantID == "" || job.UserID == "" || strings.TrimSpace(job.Content) == "" {
		return domain.Invalid("local_promote", "任务缺少租户或内容")
	}
	if s.adder == nil {
		return domain.Wrap(domain.ErrDisabled, "local_promote", "上游写入能力未启用", nil)
	}
	messages, err := json.Marshal([]domain.Message{{Role: "user", Content: job.Content}})
	if err != nil {
		return domain.Invalid("local_promote", "消息无法序列化")
	}
	_, err = s.adder.Add(ctx, domain.Caller{TenantID: job.TenantID, UserID: job.UserID}, domain.AddRequest{
		TenantID:       job.TenantID,
		UserID:         job.UserID,
		Messages:       messages,
		Tags:           job.Tags,
		Async:          false,
		CallID:         job.JobID,
		IdempotencyKey: "local_promote:" + job.JobID,
	})
	return err
}

// ReportPromoteOutcome 由 worker 在任务进入终态时调用，写回镜像状态并记审计。
func (s *Service) ReportPromoteOutcome(ctx context.Context, payload []byte, jobErr error) {
	job, err := decodePromoteJob(payload)
	if err != nil {
		s.log.WarnContext(ctx, "decode promote outcome failed", slog.String("error", err.Error()))
		return
	}
	status := domain.LocalPromoteSucceeded
	if jobErr != nil {
		status = domain.LocalPromoteFailed
	}
	if err := s.store.MarkPromoteResult(ctx, job.TenantID, job.LocalMemoryID, status, job.JobID, safeText(jobErr), s.now()); err != nil {
		s.log.WarnContext(ctx, "mark promote result failed",
			slog.String("memory_id", job.LocalMemoryID), slog.String("error", err.Error()))
	}
	rec := auditRecord("local_sync.memory.promote.background", job.TenantID, "", map[string]any{
		"memory_id": job.LocalMemoryID,
		"job_id":    job.JobID,
		"status":    status,
	}, jobErr)
	if err := s.recordNow(ctx, rec); err != nil {
		s.log.WarnContext(ctx, "record promote audit failed", slog.String("error", err.Error()))
	}
}

func decodePromoteJob(payload []byte) (promoteJob, error) {
	var job promoteJob
	decoder := json.NewDecoder(bytes.NewReader(payload))
	if err := decoder.Decode(&job); err != nil {
		return promoteJob{}, err
	}
	return job, nil
}

func promoteContent(memory domain.LocalMemory, payload map[string]any) string {
	parts := make([]string, 0, 3)
	if title := strings.TrimSpace(memory.Title); title != "" {
		parts = append(parts, title)
	}
	if summary := strings.TrimSpace(memory.Summary); summary != "" {
		parts = append(parts, summary)
	}
	if len(payload) > 0 {
		if body, err := json.MarshalIndent(payload, "", "  "); err == nil {
			parts = append(parts, string(body))
		}
	}
	return strings.TrimSpace(strings.Join(parts, "\n\n"))
}

func promoteTags(payload map[string]any, layer domain.LocalLayer) []string {
	tags := []string{"local-sync", string(layer)}
	raw, ok := payload["tags"].([]any)
	if !ok {
		return tags
	}
	seen := map[string]struct{}{"local-sync": {}, string(layer): {}}
	for _, item := range raw {
		text, ok := item.(string)
		if !ok {
			continue
		}
		text = strings.TrimSpace(text)
		if text == "" {
			continue
		}
		if _, dup := seen[text]; dup {
			continue
		}
		seen[text] = struct{}{}
		tags = append(tags, text)
	}
	return tags
}

func uniqueIDs(ids []string) []string {
	out := make([]string, 0, len(ids))
	seen := make(map[string]struct{}, len(ids))
	for _, raw := range ids {
		id := strings.TrimSpace(raw)
		if id == "" {
			continue
		}
		if _, dup := seen[id]; dup {
			continue
		}
		seen[id] = struct{}{}
		out = append(out, id)
	}
	return out
}

func failure(id string, err error) domain.LocalPromoteFailure {
	return domain.LocalPromoteFailure{ID: id, Message: safeText(err)}
}
