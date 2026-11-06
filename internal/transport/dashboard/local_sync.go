package dashboard

import (
	"context"
	"maps"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
	localsync "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/localsync"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// LocalSync 是本地插件记忆镜像端口，由 service/localsync 实现。
// 审计由 service 在动作成功后写入，Dashboard 侧不重复记录。
type LocalSync interface {
	ListDevices(ctx context.Context, tenantID string) ([]domain.LocalDevice, error)
	RevokeDevice(ctx context.Context, p domain.Principal, tenantID, deviceID string) error
	Quota(ctx context.Context, tenantID string) (domain.LocalQuota, error)
	ListMemories(ctx context.Context, tenantID string, q localsync.MemoryQuery) ([]domain.LocalMemory, int, error)
	Summary(ctx context.Context, tenantID string) (domain.LocalMemorySummary, error)
	MemoryDetail(ctx context.Context, tenantID, id string) (localsync.MemoryDetail, error)
	DeleteMemory(ctx context.Context, p domain.Principal, tenantID, id string) error
	Promote(ctx context.Context, p domain.Principal, tenantID string, ids []string) (domain.LocalPromoteResult, error)
}

// requireLocalSync 在能力未装配时返回显式 503，而不是空列表。
func (s *Server) requireLocalSync() error {
	if s.deps.LocalSync == nil {
		return disabledCapability("本地同步")
	}
	return nil
}

func (s *Server) localDevices(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	tenantID := httpx.String(body["projectId"])
	devices, err := s.deps.LocalSync.ListDevices(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	quota, err := s.deps.LocalSync.Quota(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	items := make([]map[string]any, 0, len(devices))
	for i := range devices {
		items = append(items, localDeviceJSON(devices[i]))
	}
	return map[string]any{
		"devices": items,
		"total":   len(items),
		"quota": map[string]any{
			"usedBytes":    quota.UsedBytes,
			"limitBytes":   quota.LimitBytes,
			"usedRows":     quota.UsedRows,
			"limitRows":    quota.LimitRows,
			"pendingBytes": quota.PendingBytes,
			"pendingRows":  quota.PendingRows,
		},
	}, nil
}

func localDeviceJSON(d domain.LocalDevice) map[string]any {
	return map[string]any{
		"id":         d.ID,
		"deviceId":   d.DeviceID,
		"accountId":  d.AccountID,
		"label":      d.Label,
		"agentKind":  d.AgentKind,
		"status":     string(d.Status),
		"createdAt":  d.CreatedAt.UTC().Format(time.RFC3339),
		"lastSeenAt": formatOptionalTime(d.LastSeenAt),
		"revokedAt":  formatOptionalTime(d.RevokedAt),
	}
}

func (s *Server) localDeviceRevoke(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	tenantID := httpx.String(body["projectId"])
	if err := s.deps.LocalSync.RevokeDevice(ctx, p, tenantID, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func (s *Server) localMemories(ctx context.Context, _ domain.Principal, r *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	page := num.Clamp(queryInt(r, "page", 1), 1, maxPageNum)
	size := num.Clamp(queryInt(r, "pageSize", 20), 1, 200)
	startTime, err := queryTime(r, "startTime")
	if err != nil {
		return nil, err
	}
	endTime, err := queryTime(r, "endTime")
	if err != nil {
		return nil, err
	}
	tenantID := httpx.String(body["projectId"])
	query := localsync.MemoryQuery{
		Layer:     strings.TrimSpace(r.URL.Query().Get("layer")),
		DeviceID:  strings.TrimSpace(r.URL.Query().Get("deviceId")),
		Query:     strings.TrimSpace(r.URL.Query().Get("q")),
		StartTime: startTime,
		EndTime:   endTime,
		Page:      page,
		PageSize:  size,
	}
	rows, total, err := s.deps.LocalSync.ListMemories(ctx, tenantID, query)
	if err != nil {
		return nil, err
	}
	items := make([]map[string]any, 0, len(rows))
	for i := range rows {
		items = append(items, localMemoryJSON(rows[i]))
	}
	return map[string]any{
		"records":  items,
		"total":    total,
		"pageNum":  page,
		"pageSize": size,
	}, nil
}

func (s *Server) localMemorySummary(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	summary, err := s.deps.LocalSync.Summary(ctx, httpx.String(body["projectId"]))
	if err != nil {
		return nil, err
	}
	layers := make(map[string]int64, len(summary.Layers))
	maps.Copy(layers, summary.Layers)
	return map[string]any{
		"total":          summary.Total,
		"deleted":        summary.Deleted,
		"promoted":       summary.Promoted,
		"promotePending": summary.PromotePending,
		"promoteFailed":  summary.PromoteFailed,
		"layers":         layers,
		"oldestSyncedAt": formatOptionalTime(summary.OldestSyncedAt),
		"newestSyncedAt": formatOptionalTime(summary.NewestSyncedAt),
		"quota": map[string]any{
			"usedBytes":    summary.Quota.UsedBytes,
			"limitBytes":   summary.Quota.LimitBytes,
			"usedRows":     summary.Quota.UsedRows,
			"limitRows":    summary.Quota.LimitRows,
			"pendingBytes": summary.Quota.PendingBytes,
			"pendingRows":  summary.Quota.PendingRows,
		},
	}, nil
}

func (s *Server) localMemoryDetail(ctx context.Context, _ domain.Principal, r *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	detail, err := s.deps.LocalSync.MemoryDetail(ctx, httpx.String(body["projectId"]), r.PathValue("id"))
	if err != nil {
		return nil, err
	}
	out := localMemoryJSON(detail.Memory)
	out["payload"] = detail.Payload
	return out, nil
}

func (s *Server) localMemoryDelete(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	if err := s.deps.LocalSync.DeleteMemory(ctx, p, httpx.String(body["projectId"]), r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func (s *Server) localMemoryPromote(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	if err := s.requireLocalSync(); err != nil {
		return nil, err
	}
	ids := httpx.StringList(body["ids"])
	if len(ids) == 0 {
		return nil, domain.Invalid("local_sync", "ids 不能为空")
	}
	result, err := s.deps.LocalSync.Promote(ctx, p, httpx.String(body["projectId"]), ids)
	if err != nil {
		return nil, err
	}
	return result, nil
}

// localMemoryJSON 是列表与详情共用的摘要字段；列表不读 payload，详情再补。
// promoted 以转存状态为准，不能用 promotedMemoryId 推断：上游 add 不返回记忆 ID，
// 成功转存后该字段本来就是空的。
func localMemoryJSON(m domain.LocalMemory) map[string]any {
	promoted := m.PromoteStatus == domain.LocalPromoteSucceeded
	return map[string]any{
		"id":               m.ID,
		"layer":            string(m.Layer),
		"sourceId":         m.SourceID,
		"deviceId":         m.DeviceID,
		"deviceLabel":      m.DeviceLabel,
		"contentHash":      m.ContentHash,
		"title":            m.Title,
		"summary":          m.Summary,
		"sourceUpdatedAt":  m.SourceUpdatedAt.UTC().Format(time.RFC3339),
		"syncedAt":         m.SyncedAt.UTC().Format(time.RFC3339),
		"promoted":         promoted,
		"promotedMemoryId": m.PromotedMemoryID,
		"promoteStatus":    m.PromoteStatus,
		"promoteError":     m.PromoteError,
	}
}

func formatOptionalTime(t *time.Time) string {
	if t == nil || t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339)
}

func queryInt(r *http.Request, name string, fallback int) int {
	raw := strings.TrimSpace(r.URL.Query().Get(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return value
}

// queryTime 解析 RFC3339 或 YYYY-MM-DD；空值表示不设边界。
func queryTime(r *http.Request, name string) (time.Time, error) {
	raw := strings.TrimSpace(r.URL.Query().Get(name))
	if raw == "" {
		return time.Time{}, nil
	}
	if value, err := time.Parse(time.RFC3339, raw); err == nil {
		return value, nil
	}
	if value, err := time.Parse("2006-01-02", raw); err == nil {
		return value, nil
	}
	return time.Time{}, domain.Invalid("local_sync", name+" 必须是 RFC3339 或 YYYY-MM-DD")
}
