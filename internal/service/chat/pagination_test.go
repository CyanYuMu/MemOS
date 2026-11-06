package chat

import (
	"encoding/json"
	"strconv"
	"testing"
)

type messagePage struct {
	Messages []json.RawMessage `json:"messages"`
	Start    int               `json:"start"`
	Total    int               `json:"total"`
	HasMore  bool              `json:"hasMore"`
}

func rawMessages(t *testing.T, items []string) json.RawMessage {
	t.Helper()
	messages := make([]json.RawMessage, len(items))
	for i, item := range items {
		messages[i] = json.RawMessage(item)
	}
	body, err := json.Marshal(map[string]any{"messages": messages})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return body
}

func decodePage(t *testing.T, raw json.RawMessage) messagePage {
	t.Helper()
	var page messagePage
	if err := json.Unmarshal(raw, &page); err != nil {
		t.Fatalf("unmarshal page: %v", err)
	}
	return page
}

func TestPageMessagesWalksBackwards(t *testing.T) {
	t.Parallel()
	items := make([]string, 25)
	for i := range items {
		items[i] = `{"role":"user","content":"m` + strconv.Itoa(i) + `"}`
	}
	raw := rawMessages(t, items)

	first := decodePage(t, pageMessages(raw, 10, 0))
	if first.Start != 15 || len(first.Messages) != 10 || first.Total != 25 || !first.HasMore {
		t.Fatalf("last page = start %d len %d total %d hasMore %v", first.Start, len(first.Messages), first.Total, first.HasMore)
	}

	second := decodePage(t, pageMessages(raw, 10, first.Start))
	if second.Start != 5 || len(second.Messages) != 10 || !second.HasMore {
		t.Fatalf("second page = start %d len %d hasMore %v", second.Start, len(second.Messages), second.HasMore)
	}

	last := decodePage(t, pageMessages(raw, 10, second.Start))
	if last.Start != 0 || len(last.Messages) != 5 || last.HasMore {
		t.Fatalf("first page = start %d len %d hasMore %v", last.Start, len(last.Messages), last.HasMore)
	}
}

func TestPageMessagesKeepsToolBoundary(t *testing.T) {
	t.Parallel()
	raw := rawMessages(t, []string{
		`{"role":"user","content":"start"}`,
		`{"role":"assistant","content":[{"type":"toolCall","id":"t1","name":"search_memory"}]}`,
		`{"role":"toolResult","toolCallId":"t1","content":[{"type":"text","text":"ok"}]}`,
		`{"role":"assistant","content":[{"type":"text","text":"done"}]}`,
	})

	// 取最后 2 条时，页首正好落在 toolResult 上；必须把前面的 toolCall 一起带上。
	page := decodePage(t, pageMessages(raw, 2, 0))
	if page.Start != 1 || len(page.Messages) != 3 {
		t.Fatalf("tool boundary = start %d len %d, want start 1 len 3", page.Start, len(page.Messages))
	}

	// before 指向 toolCall 之后时，页尾的 toolCall 要把 toolResult 一起带上。
	page = decodePage(t, pageMessages(raw, 1, 2))
	if page.Start != 1 || len(page.Messages) != 2 {
		t.Fatalf("tail toolCall = start %d len %d, want start 1 len 2", page.Start, len(page.Messages))
	}
}
