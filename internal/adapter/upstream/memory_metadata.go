package upstream

import (
	"encoding/json"
	"strconv"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 图库存储既有对象也有JSON字符串；逐项容错，坏历史不能掩盖当前正文。
func memoryDetails(meta map[string]any) (int, []domain.MemoryVersion, []domain.MemorySource) {
	version := metadataInt(meta["version"])
	history := []domain.MemoryVersion{}
	for _, row := range metadataRows(meta["history"]) {
		content := stringField(row, "memory")
		n := metadataInt(row["version"])
		if content == "" || n < 1 {
			continue
		}
		spec, _ := row["timespec"].(map[string]any)
		history = append(history, domain.MemoryVersion{Version: n, Content: content, CreatedAt: metadataTime(stringField(row, "created_at")), UpdateType: stringField(row, "update_type"), ValidFrom: metadataTime(stringField(spec, "valid_from")), ValidTo: metadataTime(stringField(spec, "valid_to"))})
	}
	sources := []domain.MemorySource{}
	for _, row := range metadataRows(meta["sources"]) {
		sources = append(sources, domain.MemorySource{Type: stringField(row, "type"), Role: stringField(row, "role"), Content: stringField(row, "content"), Time: metadataTime(stringField(row, "chat_time")), URL: stringField(row, "url")})
	}
	return version, history, sources
}

func metadataInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case string:
		i, _ := strconv.Atoi(n)
		return i
	default:
		return 0
	}
}

func metadataRows(v any) []map[string]any {
	if text, ok := v.(string); ok {
		if json.Unmarshal([]byte(text), &v) != nil {
			return nil
		}
	}
	rows, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]map[string]any, 0, len(rows))
	for _, raw := range rows {
		if text, ok := raw.(string); ok {
			if json.Unmarshal([]byte(text), &raw) != nil {
				continue
			}
		}
		if row, ok := raw.(map[string]any); ok {
			out = append(out, row)
		}
	}
	return out
}

func metadataTime(text string) string {
	if text == "" {
		return ""
	}
	at, err := domain.ParseUTC(text)
	if err != nil {
		return ""
	}
	return domain.BeijingClock(at.String())
}
