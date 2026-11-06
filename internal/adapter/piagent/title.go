package piagent

import (
	"context"
	"encoding/json"
	"log/slog"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// ConversationTitles 只保存入口元数据，Pi仍独占消息和上下文。
type ConversationTitles interface {
	SetChatConversationTitleIfEmpty(context.Context, string, string, string, string) (bool, error)
}

const titleInstruction = `
对话标题元数据：若历史中尚未生成 graph-memory-title，在首次最终回复末尾单独一行输出 <graph-memory-title>"简短主题"</graph-memory-title>。内容必须是一个JSON字符串，概括用户实际讨论主题，使用用户语言，通常6到18个汉字、最多40字。不要把“新对话”当标题。标题可放在graph-memory-followups前或后；两者都只放最终回复末尾，不放思考、工具参数或记忆内容。历史已有标题时无需重复。`

var titlePattern = regexp.MustCompile(`(?m)^[ \t]*<graph-memory-title>\s*("(?:[^"\\]|\\.)*")\s*</graph-memory-title>`)

type titleMessage struct {
	Role       string `json:"role"`
	StopReason string `json:"stopReason"`
	Content    []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
}

func generatedTitle(raw []byte) string {
	var history struct {
		Messages []titleMessage `json:"messages"`
	}
	if json.Unmarshal(raw, &history) != nil {
		return ""
	}
	for _, message := range history.Messages {
		if title := messageTitle(message); title != "" {
			return title
		}
	}
	return ""
}

func messageTitle(message titleMessage) string {
	if message.Role != "assistant" {
		return ""
	}
	switch message.StopReason {
	case "error", "aborted", "length", "max_tokens":
		return ""
	}
	var text strings.Builder
	for _, block := range message.Content {
		if block.Type == "toolCall" {
			return ""
		}
		if block.Type == "text" {
			text.WriteString(block.Text)
		}
	}
	match := titlePattern.FindStringSubmatch(withoutFencedCode(text.String()))
	if len(match) != 2 {
		return ""
	}
	var title string
	if json.Unmarshal([]byte(match[1]), &title) != nil {
		return ""
	}
	title = strings.TrimSpace(title)
	if title == "" || utf8.RuneCountInString(title) > 40 || strings.IndexFunc(title, unicode.IsControl) >= 0 {
		return ""
	}
	return title
}

func (s *Session) persistTitle(ctx context.Context, raw json.RawMessage) {
	spec, _ := s.specSnapshot()
	if spec.ConversationID == "" || s.rt.opts.ConversationTitles == nil {
		return
	}
	title := generatedTitle(raw)
	if title == "" {
		return
	}
	changed, err := s.rt.opts.ConversationTitles.SetChatConversationTitleIfEmpty(ctx, spec.TenantID, spec.AccountID, spec.ConversationID, title)
	if err != nil {
		s.log.WarnContext(ctx, "conversation title save failed", slog.Any("err", err))
		return
	}
	if changed {
		data, _ := json.Marshal(map[string]string{"conversationId": spec.ConversationID, "title": title})
		s.broadcast("conversation_title", data)
	}
}

// withoutFencedCode 避免把模型展示的协议示例当成对话标题。
func withoutFencedCode(text string) string {
	var out strings.Builder
	fence := ""
	for line := range strings.SplitSeq(text, "\n") {
		trim := strings.TrimSpace(line)
		if strings.HasPrefix(trim, "```") || strings.HasPrefix(trim, "~~~") {
			delimiter := strings.TrimRight(trim, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- ")
			if fence == "" {
				fence = delimiter
			} else if strings.HasPrefix(trim, fence) {
				fence = ""
			}
			continue
		}
		if fence == "" {
			out.WriteString(line)
			out.WriteByte('\n')
		}
	}
	return out.String()
}
