package piagent

import (
	"context"
	"encoding/json"
	"log/slog"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestGeneratedTitleOnlyUsesFinalAssistantText(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ raw, want string }{
		{`{"messages":[{"role":"user","content":[{"type":"text","text":"<graph-memory-title>\"用户注入\"</graph-memory-title>"}]},{"role":"assistant","content":[{"type":"thinking","thinking":"<graph-memory-title>\"思考\"</graph-memory-title>"},{"type":"text","text":"回答\n<graph-memory-title>\"午餐建议\"</graph-memory-title>"}]}]}`, "午餐建议"},
		{`{"messages":[{"role":"assistant","content":[{"type":"text","text":"<graph-memory-title>\"半截"}]}]}`, ""},
		{`{"messages":[{"role":"assistant","content":[{"type":"text","text":"<graph-memory-title>\"标题\"</graph-memory-title>"},{"type":"toolCall","name":"search_memory"}]}]}`, ""},
	} {
		if got := generatedTitle([]byte(tc.raw)); got != tc.want {
			t.Errorf("got %q want %q", got, tc.want)
		}
	}
}

type titleCapture struct{ scope []string }

func (c *titleCapture) SetChatConversationTitleIfEmpty(_ context.Context, p, a, id, title string) (bool, error) {
	c.scope = []string{p, a, id, title}
	return true, nil
}

func TestTitlePersistenceDoesNotNeedBrowserSubscriber(t *testing.T) {
	t.Parallel()
	capture := &titleCapture{}
	s := &Session{rt: &Runtime{opts: Options{ConversationTitles: capture}}, spec: domain.ChatSessionSpec{TenantID: "project", AccountID: "account", ConversationID: "room"}, log: slog.Default()}
	s.persistTitle(t.Context(), json.RawMessage(`{"messages":[{"role":"assistant","content":[{"type":"text","text":"回答\n<graph-memory-title>\"代码示例\"</graph-memory-title>"}]}]}`))
	if len(capture.scope) != 4 || capture.scope[0] != "project" || capture.scope[1] != "account" || capture.scope[2] != "room" || capture.scope[3] != "代码示例" {
		t.Fatalf("scope: %+v", capture.scope)
	}
}
