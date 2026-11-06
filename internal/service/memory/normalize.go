package memory

import (
	"crypto/sha1" //nolint:gosec // 会话标识沿用既有网关的 SHA-1 内容寻址，不是安全用途
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	sessionPrefix  = "conv_"
	quoteFollowers = ":,}]"
)

// ResolveSessionID 显式 session_id 优先，否则用会话首条消息的哈希。
// content-addressing 只是老客户端的兼容退路：不同会话开场白相同会坍缩成一个 id。
func ResolveSessionID(explicit, firstMessage string) string {
	if explicit = strings.TrimSpace(explicit); explicit != "" {
		return explicit
	}
	text := strings.TrimSpace(firstMessage)
	if text == "" {
		return ""
	}
	sum := sha1.Sum([]byte(text)) //nolint:gosec // 同上：只做内容寻址，不用于认证
	return sessionPrefix + hex.EncodeToString(sum[:])[:16]
}

// NormalizeMessages 把 MCP/REST 传来的 messages 归一成上游可接受的 []Message。
//
// 客户端常见两种畸形入参：整个 messages 是 JSON 字符串，或数组里装的是 JSON 字符串。
// 两者都展开；展开不了就报错，不静默丢消息。
func NormalizeMessages(raw json.RawMessage) ([]domain.Message, error) {
	if len(strings.TrimSpace(string(raw))) == 0 {
		return nil, domain.Invalid("normalize_messages", "messages must not be empty")
	}
	value, err := decodeLenient(string(raw))
	if err != nil {
		return nil, domain.Wrap(domain.ErrInvalidInput, "normalize_messages", "messages 不是合法 JSON", err)
	}
	// MCP 的 arguments 是通用 map，字符串会被 json.Marshal 再包一层引号，
	// 所以整个 messages 是 JSON 字符串时会解析成 string；这里再展开一次。
	if text, ok := value.(string); ok {
		if decoded, err := decodeLenient(text); err == nil {
			value = decoded
		}
	}
	items, ok := value.([]any)
	if !ok {
		return nil, domain.Invalid("normalize_messages", "messages 必须是数组")
	}

	flattened := flattenMessageObjects(items)
	out := make([]domain.Message, 0, len(flattened))
	for _, m := range flattened {
		out = append(out, normalizeOne(m))
	}
	if len(out) == 0 {
		return nil, domain.Invalid("normalize_messages",
			"messages 里没有可用的消息对象；每项应为 {\"role\": \"user\"|\"assistant\", \"content\": \"...\"}")
	}
	return out, nil
}

// flattenMessageObjects 展开对象、JSON 字符串与字符串数组三种形态。
func flattenMessageObjects(items []any) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		switch typed := item.(type) {
		case map[string]any:
			out = append(out, typed)
		case string:
			decoded, err := decodeLenient(typed)
			if err != nil {
				continue
			}
			switch d := decoded.(type) {
			case map[string]any:
				out = append(out, d)
			case []any:
				for _, sub := range d {
					if m, ok := sub.(map[string]any); ok {
						out = append(out, m)
					}
				}
			}
		}
	}
	return out
}

func normalizeOne(m map[string]any) domain.Message {
	msg := domain.Message{
		Role:       strings.TrimSpace(stringValue(m["role"])),
		Content:    normalizeContent(m["content"]),
		ToolCalls:  normalizeToolCalls(m["tool_calls"]),
		ToolCallID: strings.TrimSpace(stringValue(m["tool_call_id"])),
		Name:       strings.TrimSpace(stringValue(m["name"])),
	}
	if msg.Role == "" {
		msg.Role = "user"
	}
	return msg
}

func normalizeContent(v any) any {
	switch typed := v.(type) {
	case nil:
		return ""
	case string:
		return typed
	case []any:
		return typed
	default:
		encoded, err := json.Marshal(typed)
		if err != nil {
			return fmt.Sprint(typed)
		}
		return string(encoded)
	}
}

func normalizeToolCalls(v any) []domain.ToolCall {
	list, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]domain.ToolCall, 0, len(list))
	for _, item := range list {
		tc, ok := item.(map[string]any)
		if !ok {
			continue
		}
		fn, ok := tc["function"].(map[string]any)
		if !ok || strings.TrimSpace(stringValue(fn["name"])) == "" {
			continue
		}
		call := domain.ToolCall{
			ID:   stringValue(tc["id"]),
			Type: stringValue(tc["type"]),
			Function: domain.ToolCallFunction{
				Name:      stringValue(fn["name"]),
				Arguments: stringValue(fn["arguments"]),
			},
		}
		if call.Type == "" {
			call.Type = "function"
		}
		if call.Function.Arguments == "" {
			call.Function.Arguments = "{}"
		}
		out = append(out, call)
	}
	return out
}

func stringValue(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

// decodeLenient 先按原样解析，失败再修一次字符串内未转义的引号。
// 线上实测内容里自带引号会让整段 JSON 不合法。
func decodeLenient(text string) (any, error) {
	var value any
	if err := json.Unmarshal([]byte(text), &value); err == nil {
		return value, nil
	}
	repaired := repairUnescapedQuotes(text)
	if err := json.Unmarshal([]byte(repaired), &value); err != nil {
		return nil, err
	}
	return value, nil
}

func repairUnescapedQuotes(text string) string {
	var b strings.Builder
	b.Grow(len(text) + 8)
	inString := false
	for i := 0; i < len(text); i++ {
		ch := text[i]
		if inString && ch == '\\' && i+1 < len(text) {
			b.WriteByte(ch)
			b.WriteByte(text[i+1])
			i++
			continue
		}
		if ch != '"' {
			b.WriteByte(ch)
			continue
		}
		if !inString {
			inString = true
			b.WriteByte(ch)
			continue
		}
		j := i + 1
		for j < len(text) && strings.ContainsRune(" \t\r\n", rune(text[j])) {
			j++
		}
		if j < len(text) && strings.ContainsRune(quoteFollowers, rune(text[j])) {
			inString = false
			b.WriteByte(ch)
		} else {
			b.WriteString(`\"`)
		}
	}
	return b.String()
}
