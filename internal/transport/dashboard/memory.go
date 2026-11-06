package dashboard

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func (s *Server) memoryDetail(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	memoryID := strings.TrimSpace(httpx.String(body["memoryId"]))
	if projectID == "" {
		return nil, domain.Invalid("dashboard", "缺少 projectId")
	}
	if memoryID == "" {
		return nil, domain.Invalid("dashboard", "缺少 memoryId")
	}
	if httpx.String(body["memType"]) == "event_mem" {
		if s.deps.Graph == nil {
			return nil, disabledCapability("图库")
		}
		mem, err := s.deps.Graph.MemoryBody(ctx, projectID, memoryID)
		if err != nil {
			return nil, err
		}
		// 与列表同口径：上游缺 user_id 时回退到租户，subjectType 由 user_id 与租户的关系推。
		userID := mem.UserID
		if userID == "" {
			userID = projectID
		}
		return map[string]any{
			"memoryId":       memoryID,
			"memoryContent":  mem.Content,
			"subjectType":    subjectTypeOf(userID, projectID),
			"externalUserId": userID,
			"createTime":     domain.BeijingClock(mem.CreatedAt),
			"updateTime":     mem.UpdatedAt, "memoryType": mem.Type, "version": mem.Version, "history": mem.History, "sources": mem.Sources,
		}, nil
	}
	if s.deps.Memories == nil {
		return nil, disabledCapability("上游")
	}
	tenant, err := s.deps.Tenants.Get(ctx, projectID)
	if err != nil {
		return nil, err
	}
	read := s.deps.Memories.OwnedMemory
	if detail, ok := s.deps.Memories.(interface {
		MemoryDetail(context.Context, string, string, string) (domain.Memory, bool, error)
	}); ok {
		read = detail.MemoryDetail
	}
	mem, owned, err := read(ctx, projectID, tenant.UserID, memoryID)
	if err != nil {
		return nil, err
	}
	if !owned {
		return nil, domain.NotFound("dashboard", "记忆不存在")
	}
	return memoryDetailJSON(mem), nil
}

func (s *Server) memoryDelete(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	ids := httpx.StringList(body["memory_ids"])
	if projectID == "" {
		return nil, domain.Invalid("dashboard", "缺少 projectId")
	}
	if len(ids) == 0 {
		return nil, domain.Invalid("dashboard", "缺少 memory_ids")
	}
	if httpx.String(body["memType"]) == "event_mem" {
		return nil, domain.Invalid("dashboard", "事件记忆来自图库，当前只读，不支持删除")
	}
	tenant, err := s.deps.Tenants.Get(ctx, projectID)
	if err != nil {
		return nil, err
	}
	result, err := s.deps.Memory.Delete(ctx, domain.Caller{TenantID: projectID, UserID: tenant.UserID}, ids)
	if err != nil {
		return nil, err
	}
	out := map[string]any{"deleted": len(result.DeletedIDs)}
	if len(result.NotFoundIDs) > 0 {
		out["not_found_ids"] = result.NotFoundIDs
	}
	return out, nil
}

func memoryJSON(m domain.Memory) map[string]any {
	tags := append([]string{}, m.Tags...)
	out := map[string]any{
		"memoryId":       m.ID,
		"memoryContent":  m.Content,
		"subjectType":    "user",
		"externalUserId": m.UserID,
		"createTime":     domain.BeijingClockOf(m.CreatedAt),
		"memoryKey":      m.Key,
		"memoryType":     m.NodeType,
		"status":         m.Status,
		"tags":           tags,
		"updateTime":     domain.BeijingClockOf(m.UpdatedAt),
	}
	// 画像与旧快照的零值表示没有检索评分，避免伪造相关度。
	if m.Relativity != 0 {
		out["relativity"] = m.Relativity
	}
	return out
}

func (s *Server) memoryActivity(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	if s.deps.Audit == nil {
		return nil, disabledCapability("记忆日志")
	}
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	tenant, err := s.deps.Tenants.Get(ctx, projectID)
	if err != nil {
		return nil, err
	}
	caller := domain.Caller{TenantID: projectID, UserID: tenant.UserID}
	ids := httpx.StringList(body["callIds"])
	var activities []domain.MemoryActivity
	cursor := httpx.String(body["after"])
	if len(ids) > 0 {
		activities, err = s.deps.Memory.Activity(ctx, caller, ids, s.deps.Audit)
	} else {
		activities, err = s.deps.Memory.RecentActivity(ctx, caller, cursor, s.deps.Audit)
	}
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(activities))
	latest, _ := time.Parse(time.RFC3339Nano, cursor)
	for _, activity := range activities {
		if at, parseErr := time.Parse(time.RFC3339Nano, activity.CompletedAt); parseErr == nil && at.After(latest) {
			latest = at
			cursor = activity.CompletedAt
		}
		memories := make([]map[string]any, 0, len(activity.Memories))
		for i := range activity.Memories {
			memories = append(memories, memoryDetailJSON(activity.Memories[i]))
		}
		records = append(records, map[string]any{"callId": activity.CallID, "status": activity.Status, "completedAt": activity.CompletedAt, "memories": memories, "total": activity.Total, "truncated": activity.Truncated})
	}
	return map[string]any{"records": records, "total": len(records), "cursor": cursor}, nil
}

func memoryDetailJSON(m domain.Memory) map[string]any {
	out := memoryJSON(m)
	if m.Version > 0 {
		out["version"] = m.Version
	}
	if m.History != nil {
		out["history"] = m.History
	}
	if m.Sources != nil {
		out["sources"] = m.Sources
	}
	return out
}
