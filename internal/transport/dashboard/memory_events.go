package dashboard

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func (s *Server) memoryEvents(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	project := strings.TrimSpace(httpx.String(body["projectId"]))
	tenant, err := s.deps.Tenants.Get(ctx, project)
	if err != nil {
		return nil, err
	}
	ids, err := memoryEventCallIDs(body)
	if err != nil {
		return nil, err
	}
	page, err := s.deps.Memory.Events(ctx, domain.Caller{TenantID: project, UserID: tenant.UserID}, httpx.String(body["after"]), ids)
	if err != nil {
		return nil, err
	}
	rows := make([]map[string]any, 0, len(page.Records))
	for i := range page.Records {
		rows = append(rows, memoryEventJSON(page.Records[i]))
	}
	return map[string]any{"records": rows, "cursor": page.Cursor, "hasMore": page.HasMore, "total": len(rows)}, nil
}

func memoryEventCallIDs(body map[string]any) ([]string, error) {
	value, exists := body["callIds"]
	if !exists || value == nil {
		return nil, nil
	}
	values, ok := value.([]any)
	if !ok || len(values) > 20 {
		return nil, domain.Invalid("memory_events", "请选择有效的记忆日志后重试")
	}
	ids := make([]string, 0, len(values))
	for _, value := range values {
		id, ok := value.(string)
		if !ok || strings.TrimSpace(id) == "" || len(id) > 256 {
			return nil, domain.Invalid("memory_events", "请选择有效的记忆日志后重试")
		}
		ids = append(ids, id)
	}
	return ids, nil
}

func memoryEventJSON(event domain.MemoryEvent) map[string]any {
	memories, before := []map[string]any{}, []map[string]any{}
	for i := range event.Memories {
		memories = append(memories, memoryDetailJSON(event.Memories[i]))
	}
	for i := range event.Before {
		before = append(before, memoryDetailJSON(event.Before[i]))
	}
	return map[string]any{
		"eventId": event.ID, "sequence": strconv.FormatInt(event.Sequence, 10), "kind": event.Kind, "status": event.Status,
		"callId": event.CallID, "sessionId": event.SessionID, "completedAt": event.OccurredAt.UTC().Format(time.RFC3339Nano),
		"memories": memories, "before": before, "memoryIds": event.IDs, "total": event.Total, "truncated": event.Truncated,
	}
}
