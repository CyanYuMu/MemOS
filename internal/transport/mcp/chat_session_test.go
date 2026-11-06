package mcp

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
)

func TestChatRuntimeSessionMetadataOverridesModelGuess(t *testing.T) {
	t.Parallel()
	id := "chat-0123456789abcdef01234567"
	if got := sessionIDFor(withChatSession(t.Context(), id), "model-guessed"); got != id {
		t.Fatal(got)
	}
	for _, invalid := range []string{"", "foreign", "chat-../../foreign", "chat-0123456789abcdef0123456Z"} {
		if got := sessionIDFor(withChatSession(context.Background(), invalid), "explicit-client"); got != "explicit-client" {
			t.Fatal(got)
		}
	}
}

type chatEvents struct{ rows []domain.MemoryEvent }

func (f *chatEvents) AppendMemoryEvent(_ context.Context, e domain.MemoryEvent) error {
	f.rows = append(f.rows, e)
	return nil
}

func (*chatEvents) ListMemoryEvents(context.Context, string, int64, int, bool) ([]domain.MemoryEvent, error) {
	return nil, nil
}

func (*chatEvents) MemoryEventsByCalls(context.Context, string, []string) ([]domain.MemoryEvent, error) {
	return nil, nil
}

func TestHTTPChatSessionFlowsToUpstreamAuditAndEvents(t *testing.T) {
	t.Parallel()
	token := "test-only"
	up := &fakeUpstream{}
	events := &chatEvents{}
	audits := &captureAuditStore{}
	recorder, err := audit.New(audits, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = recorder.Stop(context.Background()) })
	srv := newTestServerWith(t, up, &token, nil, recorder, memory.Options{Events: events})
	id := "chat-0123456789abcdef01234567"
	body := `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_memory","arguments":{"query":"tea","session_id":"model-guess"}}}`
	send := func(auth string) *httptest.ResponseRecorder {
		req := httptest.NewRequestWithContext(t.Context(), http.MethodPost, "/mcp", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/json, text/event-stream")
		req.Header.Set("X-GraphMemory-Chat-Session", id)
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		return rec
	}
	if got := send(""); got.Code != 401 {
		t.Fatalf("session header bypassed auth: %d", got.Code)
	}
	if got := send(token); got.Code != 200 {
		t.Fatalf("status%d %s", got.Code, got.Body.String())
	}
	if up.gotSearch.SessionID != id {
		t.Fatalf("upstream session=%q", up.gotSearch.SessionID)
	}
	if err := recorder.Stop(t.Context()); err != nil {
		t.Fatal(err)
	}
	rows := audits.records()
	if len(rows) != 1 || rows[0].SessionID != id {
		t.Fatalf("audit: %+v", rows)
	}
	if len(events.rows) != 1 || events.rows[0].SessionID != id || events.rows[0].CallID != rows[0].CallID {
		t.Fatalf("event: %+v", events.rows)
	}
}
