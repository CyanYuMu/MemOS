package memory

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// EventStore 与运维审计分开，事件正文不会被审计文本截断或定期清理。
type EventStore interface {
	AppendMemoryEvent(context.Context, domain.MemoryEvent) error
	ListMemoryEvents(context.Context, string, int64, int, bool) ([]domain.MemoryEvent, error)
	MemoryEventsByCalls(context.Context, string, []string) ([]domain.MemoryEvent, error)
}

type (
	eventScope    struct{ callID, sessionID string }
	eventScopeKey struct{}
)

// WithEventScope 的调用标识来自入口生成值，只用于关联，不赋予读取或修改权限。
func WithEventScope(ctx context.Context, callID, sessionID string) context.Context {
	return context.WithValue(ctx, eventScopeKey{}, eventScope{callID, sessionID})
}

func (s *Service) recordEvent(ctx context.Context, caller domain.Caller, event domain.MemoryEvent) {
	if s.opts.Events == nil || requireTenant(caller) != nil {
		return
	}
	scope, _ := ctx.Value(eventScopeKey{}).(eventScope)
	if event.CallID == "" {
		event.CallID = scope.callID
	}
	if event.CallID == "" {
		event.CallID = domain.NewID()
	}
	if event.SessionID == "" {
		event.SessionID = scope.sessionID
	}
	event.ID, event.TenantID, event.UserID = domain.NewID(), caller.TenantID, caller.UserID
	event.Key = event.Kind + ":" + event.CallID + ":" + event.Status
	event.OccurredAt = domain.UTCTime{Time: time.Now().UTC()}
	boundEvent(&event)
	writeCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	if err := s.opts.Events.AppendMemoryEvent(writeCtx, event); err != nil {
		s.log.WarnContext(ctx, "memory event unavailable", slog.String("kind", event.Kind), slog.String("call_id", event.CallID))
	}
}

// 整体按字节预算舍弃完整实体，不能把截断字段当成真实记忆。
func boundEvent(event *domain.MemoryEvent) {
	ids := uniqueMemoryIDs(event.IDs)
	event.Total = len(ids)
	event.IDs = []string{}
	for _, id := range ids[:min(len(ids), maxSnapshotMemories)] {
		if len(id) <= 256 {
			event.IDs = append(event.IDs, id)
		}
	}
	if len(event.IDs) != len(ids) {
		event.Truncated = true
	}
	if len(event.SessionID) > 1024 {
		event.SessionID = ""
		event.Truncated = true
	}
	memories, before := event.Memories, event.Before
	event.Memories, event.Before = []domain.Memory{}, []domain.Memory{}
	appendEventMemories(event, memories, false)
	appendEventMemories(event, before, true)
}

func appendEventMemories(event *domain.MemoryEvent, rows []domain.Memory, before bool) {
	seen := map[string]bool{}
	confirmed := map[string]bool{}
	for _, id := range event.IDs {
		confirmed[id] = true
	}
	for i := range rows {
		mem := rows[i]
		if seen[mem.ID] {
			continue
		}
		seen[mem.ID] = true
		if !confirmed[mem.ID] || (mem.TenantID != "" && mem.TenantID != event.TenantID) || (mem.UserID != "" && mem.UserID != event.UserID) {
			event.Truncated = true
			continue
		}
		mem.TenantID, mem.UserID = event.TenantID, event.UserID
		mem.History, mem.Sources = nil, nil
		mem.Tags = append([]string(nil), mem.Tags...)
		destination := &event.Memories
		if before {
			destination = &event.Before
		}
		*destination = append(*destination, mem)
		body, err := json.Marshal(event)
		if err != nil || len(body) > 192<<10 {
			*destination = (*destination)[:len(*destination)-1]
			event.Truncated = true
		}
	}
}

type eventCursor struct {
	Tenant string `json:"tenant"`
	Seq    int64  `json:"seq"`
}

// Events 游标包含作用域；同时间戳事件按持久化序号完整追上，不用时间窗口猜关联。
func (s *Service) Events(ctx context.Context, caller domain.Caller, after string, calls ...[]string) (domain.MemoryEventPage, error) {
	page := domain.MemoryEventPage{Records: []domain.MemoryEvent{}}
	if err := requireTenant(caller); err != nil {
		return page, err
	}
	if s.opts.Events == nil {
		return page, domain.Wrap(domain.ErrDisabled, "memory_events", "暂时无法读取记忆日志，请稍后重试", nil)
	}
	if len(calls) > 0 && len(calls[0]) > 0 {
		return s.eventsByCalls(ctx, caller, after, calls[0])
	}
	cursor := eventCursor{Tenant: caller.TenantID}
	if len(after) > 512 {
		return page, domain.Invalid("memory_events", "记忆日志位置无效，请刷新后重试")
	}
	if after != "" {
		body, err := base64.RawURLEncoding.DecodeString(after)
		if len(after) > 512 || err != nil || json.Unmarshal(body, &cursor) != nil || cursor.Tenant != caller.TenantID || cursor.Seq < 0 {
			return page, domain.Invalid("memory_events", "记忆日志位置无效，请刷新后重试")
		}
	}
	rows, err := s.opts.Events.ListMemoryEvents(ctx, caller.TenantID, cursor.Seq, 51, after == "")
	if err != nil {
		return page, err
	}
	page.HasMore = after != "" && len(rows) > 50
	page.Records = rows[:min(len(rows), 50)]
	for i := range page.Records {
		cursor.Seq = max(cursor.Seq, page.Records[i].Sequence)
	}
	body, _ := json.Marshal(cursor)
	page.Cursor = base64.RawURLEncoding.EncodeToString(body)
	return page, nil
}

func (s *Service) eventsByCalls(ctx context.Context, caller domain.Caller, after string, ids []string) (domain.MemoryEventPage, error) {
	page := domain.MemoryEventPage{Records: []domain.MemoryEvent{}}
	if after != "" || len(ids) > 20 {
		return page, domain.Invalid("memory_events", "请选择有效的记忆日志后重试")
	}
	for _, id := range ids {
		if id == "" || len(id) > 256 {
			return page, domain.Invalid("memory_events", "请选择有效的记忆日志后重试")
		}
	}
	rows, err := s.opts.Events.MemoryEventsByCalls(ctx, caller.TenantID, uniqueMemoryIDs(ids))
	if err != nil {
		return page, err
	}
	page.Records = rows
	return page, nil
}
