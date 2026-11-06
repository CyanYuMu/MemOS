// Package response 把领域结果映射成 MCP 工具与 REST 接口共用的响应体字段。
// 两条入口共用同一份映射，保证输出一致。
package response

import (
	"encoding/json"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Search 生成检索结果；includeDream 决定是否带 dream_memory_detail_list。
func Search(result domain.SearchResult, includeDream bool) map[string]any {
	out := map[string]any{
		"memory_detail_list":      facts(result.Facts),
		"preference_detail_list":  preferences(result.Preferences),
		"tool_memory_detail_list": tools(result.Tools),
	}
	if includeDream {
		out["dream_memory_detail_list"] = dreams(result.Dreams)
	}
	return out
}

// Profile 生成画像结果。
func Profile(result domain.ProfileResult) map[string]any {
	out := Search(domain.SearchResult{
		Facts:       result.Facts,
		Preferences: result.Preferences,
		Tools:       result.Tools,
	}, false)
	out["current"] = result.Current
	out["size"] = result.Size
	out["total"] = result.Total
	return out
}

// Add 生成写入结果。
func Add(result domain.AddResult) map[string]any {
	status := result.Status
	if status == "" {
		status = "success"
	}
	message := result.Message
	if message == "" {
		message = "Memory added successfully"
	}
	out := map[string]any{"status": status, "message": message}
	// 异步只代表“已入队”。call_id 让调用方（以及排查的人）能在任务表/死信页
	// 找到这条写入；同步写入已经落库，没有可追踪的任务，故不带这两个字段。
	if result.Deferred {
		out["deferred"] = true
		out["call_id"] = result.CallID
	}
	return out
}

// Delete 生成删除结果。
func Delete(result domain.DeleteResult) map[string]any {
	deleted := append([]string(nil), result.DeletedIDs...)
	if deleted == nil {
		deleted = []string{}
	}
	out := map[string]any{"status": "success", "deleted_ids": deleted}
	// 线上只在有 missing 或一条都没删时给 not_found_ids（后者即使是空列表也给）。
	if len(result.NotFoundIDs) > 0 || len(result.DeletedIDs) == 0 {
		missing := append([]string(nil), result.NotFoundIDs...)
		if missing == nil {
			missing = []string{}
		}
		out["not_found_ids"] = missing
	}
	if len(result.DeletedIDs) == 0 {
		out["message"] = "没有可删除的记忆（id 不存在或已删除）"
	} else {
		out["message"] = "Memories deleted successfully"
	}
	return out
}

// Update 返回读回确认的记忆，保持检索的投影形状让日志使用同一详情入口。
func Update(result domain.MemoryUpdateResult) map[string]any {
	rows := facts([]domain.Memory{result.Memory})
	row := rows[0]
	row["memory_type"], row["status"] = result.Memory.NodeType, result.Memory.Status
	if !result.Memory.UpdatedAt.IsZero() {
		row["updated_at"] = result.Memory.UpdatedAt
	}
	return map[string]any{"status": "success", "changed": result.Changed, "version": result.Memory.Version, "memory_detail_list": rows}
}

// Feedback 生成反馈结果。
func Feedback(result domain.FeedbackResult) map[string]any {
	status := result.Status
	if status == "" {
		status = "success"
	}
	message := result.Message
	if message == "" {
		// 与线上网关一致：成功即 "Feedback recorded"，不附加“是否有改动”的判断。
		message = "Feedback recorded"
	}
	added := make([]map[string]any, 0, len(result.Added))
	for _, change := range result.Added {
		item := map[string]any{"id": change.ID}
		if change.Text != "" {
			item["text"] = change.Text
		}
		added = append(added, item)
	}
	updated := make([]map[string]any, 0, len(result.Updated))
	for _, change := range result.Updated {
		item := map[string]any{"id": change.ID}
		if change.Text != "" {
			item["text"] = change.Text
		}
		if change.ArchivedID != "" {
			item["archived_id"] = change.ArchivedID
		}
		updated = append(updated, item)
	}
	out := map[string]any{
		"status":  status,
		"message": message,
		"added":   added,
		"updated": updated,
	}
	if result.Truncated {
		out["truncated"] = true
	}
	return out
}

// Encode 序列化为稳定 JSON：去 nil、缩进 1 个空格（与既有网关 dump_json 一致）。
func Encode(v any) string {
	clean := dropNil(v)
	raw, err := json.MarshalIndent(clean, "", " ")
	if err != nil {
		return "{}"
	}
	return string(raw)
}

func facts(items []domain.Memory) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for i := range items {
		item := &items[i]
		row := base(*item)
		row["memory_key"] = item.Key
		row["memory_value"] = item.Content
		out = append(out, row)
	}
	return out
}

func dreams(items []domain.Memory) []map[string]any {
	return facts(items)
}

func preferences(items []domain.Memory) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for i := range items {
		item := &items[i]
		row := base(*item)
		row["preference"] = item.Content
		row["preference_type"] = item.Subtype
		out = append(out, row)
	}
	return out
}

func tools(items []domain.Memory) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for i := range items {
		item := &items[i]
		row := base(*item)
		row["tool_value"] = item.Content
		out = append(out, row)
	}
	return out
}

func base(item domain.Memory) map[string]any {
	row := map[string]any{
		"relativity": item.Relativity,
		"memory_id":  item.ID,
	}
	if !item.CreatedAt.IsZero() {
		row["create_time"] = item.CreatedAt
	}
	if len(item.Tags) > 0 {
		row["tags"] = item.Tags
	}
	return row
}

func dropNil(v any) any {
	switch typed := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(typed))
		for k, value := range typed {
			if value == nil {
				continue
			}
			out[k] = dropNil(value)
		}
		return out
	case []map[string]any:
		out := make([]any, 0, len(typed))
		for _, item := range typed {
			out = append(out, dropNil(item))
		}
		return out
	case []string:
		return typed
	default:
		return v
	}
}
