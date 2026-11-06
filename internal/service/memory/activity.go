package memory

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ActivityReader 保持读审计与读记忆分离，不让页面借结果ID绕过项目归属检查。
type ActivityReader interface {
	List(context.Context, domain.AuditFilter) ([]domain.AuditRecord, int, error)
	Get(context.Context, string) (domain.AuditRecord, error)
}

// Activity 只读取写入确认的结果，不用当前总量或消息内容推断新增记录。
func (s *Service) Activity(ctx context.Context, caller domain.Caller, ids []string, reader ActivityReader) ([]domain.MemoryActivity, error) {
	if err := requireTenant(caller); err != nil {
		return nil, err
	}
	if len(ids) == 0 || len(ids) > 20 {
		return nil, domain.Invalid("memory_activity", "每次查询1至20条记忆日志")
	}
	if reader == nil {
		return nil, errors.New("memory activity: audit reader unavailable")
	}
	out := make([]domain.MemoryActivity, 0, len(ids))
	seen := make(map[string]bool)
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id == "" || len(id) > 128 {
			return nil, domain.Invalid("memory_activity", "记忆日志标识无效")
		}
		if seen[id] {
			continue
		}
		seen[id] = true
		entry, err := s.activity(ctx, caller, id, reader)
		if err != nil {
			return nil, err
		}
		out = append(out, entry)
	}
	return out, nil
}

func (s *Service) activity(ctx context.Context, caller domain.Caller, id string, reader ActivityReader) (domain.MemoryActivity, error) {
	result := domain.MemoryActivity{CallID: id, Status: "not_found", Memories: []domain.Memory{}}
	filter := domain.AuditFilter{TenantID: caller.TenantID, CallID: id, Tool: domain.ToolAddMessageBackground, Limit: 1}
	records, _, err := reader.List(ctx, filter)
	if err != nil {
		return result, err
	}
	if len(records) == 0 {
		result.Status, err = activityRequestStatus(ctx, reader, filter)
		return result, err
	}

	record := records[0]
	return s.readCompletedActivity(ctx, caller, record, reader)
}

// RecentActivity 让刷新页面和其它入口写入也能看到结果；游标避免重复读取记忆正文。
func (s *Service) RecentActivity(ctx context.Context, caller domain.Caller, after string, reader ActivityReader) ([]domain.MemoryActivity, error) {
	if err := requireTenant(caller); err != nil {
		return nil, err
	}
	if reader == nil {
		return nil, errors.New("memory activity: audit reader unavailable")
	}
	filter := domain.AuditFilter{TenantID: caller.TenantID, Tool: domain.ToolAddMessageBackground, Limit: 20}
	if after != "" {
		at, err := time.Parse(time.RFC3339Nano, after)
		if err != nil {
			return nil, domain.Invalid("memory_activity", "记忆日志时间无效")
		}
		filter.Since = at.Add(time.Nanosecond)
	}
	records, _, err := reader.List(ctx, filter)
	if err != nil {
		return nil, err
	}
	results := make([]domain.MemoryActivity, 0, len(records))
	for i := range records {
		entry, readErr := s.readCompletedActivity(ctx, caller, records[i], reader)
		if readErr != nil {
			return nil, readErr
		}
		results = append(results, entry)
	}
	return results, nil
}

// 列表为了避免大正文占内存只返回元数据，必须经详情读取已持久化的结果。
func (s *Service) readCompletedActivity(ctx context.Context, caller domain.Caller, metadata domain.AuditRecord, reader ActivityReader) (domain.MemoryActivity, error) {
	record, err := reader.Get(ctx, metadata.ID)
	if err != nil {
		return domain.MemoryActivity{}, err
	}
	if record.TenantID != caller.TenantID || record.CallID != metadata.CallID || record.Tool != domain.ToolAddMessageBackground {
		return domain.MemoryActivity{}, domain.NotFound("memory_activity", "记忆日志不存在")
	}
	return completedActivity(caller, record), nil
}

func completedActivity(caller domain.Caller, record domain.AuditRecord) domain.MemoryActivity {
	result := domain.MemoryActivity{CallID: record.CallID, Memories: []domain.Memory{}}
	result.CompletedAt = record.TS.UTC().Format(time.RFC3339Nano)
	result.Status = "failed"
	if !record.OK {
		return result
	}
	result.Status = "unavailable"
	var output domain.MemoryWriteOutput
	if json.Unmarshal([]byte(record.Output), &output) != nil || output.MemoryIDs == nil {
		return result
	}
	unique := uniqueMemoryIDs(output.MemoryIDs)
	result.Total = len(unique)
	if result.Total == 0 {
		result.Status = "succeeded"
		return result
	}
	snapshot, valid := activitySnapshot(caller, output)
	if !valid {
		return result
	}
	result.Status = "succeeded"
	result.Memories = snapshot.Memories
	result.Truncated = snapshot.Truncated || len(result.Memories) != result.Total
	return result
}

func activityRequestStatus(ctx context.Context, reader ActivityReader, filter domain.AuditFilter) (string, error) {
	filter.Tool = domain.ToolAddMessage
	rows, _, err := reader.List(ctx, filter)
	if err != nil {
		return "not_found", err
	}
	if len(rows) == 0 {
		return "not_found", nil
	}
	switch rows[0].Status {
	case domain.AuditStatusRunning:
		return "pending", nil
	case domain.AuditStatusFailed:
		return "failed", nil
	default:
		return "unavailable", nil
	}
}
