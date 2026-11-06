package chat_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/chat"
	chattransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/chat"
)

func TestArchiveConversationHTTPChecksOwnerAndPreservesRestore(t *testing.T) {
	t.Parallel()
	s := &archiveConversations{}
	session := &fakeSession{}
	svc, err := chat.New(&fakeRuntime{session: session}, fakeConfigs{}, fakeProjects{}, fakeTokens{}, chat.Options{Conversations: s, ContextWindow: 131072, MaxTokens: 16384})
	if err != nil {
		t.Fatal(err)
	}
	server, err := chattransport.New(chattransport.Deps{Chat: svc, Accounts: fakeAuth{}})
	if err != nil {
		t.Fatal(err)
	}
	request := func(id, body string, status int) {
		t.Helper()
		r := httptest.NewRequestWithContext(t.Context(), http.MethodPut, "/api/chat/conversations/"+id+"/archive", strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		server.Handler().ServeHTTP(w, r)
		if w.Code != status {
			t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
		}
	}
	request("own", `{"projectId":"p1"}`, http.StatusBadRequest)
	request("own", `{"projectId":"p2","archived":true}`, http.StatusForbidden)
	request("foreign", `{"projectId":"p1","archived":true}`, http.StatusNotFound)
	request("own", `{"projectId":"p1","archived":true}`, http.StatusOK)
	if !s.archived {
		t.Fatal("archive did not reach store")
	}
	request("own", `{"projectId":"p1","archived":false}`, http.StatusOK)
	if s.archived {
		t.Fatal("restore did not reach store")
	}
	if session.calls.Load() != 0 {
		t.Fatal("archive started a model turn")
	}
}

type archiveConversations struct{ archived bool }

func (*archiveConversations) CreateChatConversation(context.Context, domain.ChatConversation) error {
	return nil
}

func (*archiveConversations) GetChatConversation(context.Context, string, string, string) (domain.ChatConversation, error) {
	return domain.ChatConversation{}, domain.NotFound("chat", "对话")
}

func (*archiveConversations) ListChatConversations(context.Context, string, string, int) ([]domain.ChatConversation, int, error) {
	return nil, 0, nil
}

func (s *archiveConversations) SetChatConversationArchived(_ context.Context, project, account, id string, archived bool) error {
	if project != "p1" || account != "a1" || id != "own" {
		return domain.NotFound("chat", "对话")
	}
	s.archived = archived
	return nil
}
