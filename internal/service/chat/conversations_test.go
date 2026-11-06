package chat_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/chat"
)

type fakeConversations struct {
	rows     []domain.ChatConversation
	archived map[string]bool
}

func (f *fakeConversations) CreateChatConversation(_ context.Context, c domain.ChatConversation) error {
	f.rows = append(f.rows, c)
	return nil
}

func (f *fakeConversations) GetChatConversation(_ context.Context, p, a, id string) (domain.ChatConversation, error) {
	for _, c := range f.rows {
		if c.TenantID == p && c.AccountID == a && c.ID == id && !f.archived[id] {
			return c, nil
		}
	}
	return domain.ChatConversation{}, domain.NotFound("test", "对话")
}

func (f *fakeConversations) ListChatConversations(_ context.Context, p, a string, _ int) ([]domain.ChatConversation, int, error) {
	rows := make([]domain.ChatConversation, 0)
	for _, c := range f.rows {
		if c.TenantID == p && c.AccountID == a && !f.archived[c.ID] {
			rows = append(rows, c)
		}
	}
	return rows, len(rows), nil
}

func TestConversationSelectionBeforeRuntime(t *testing.T) {
	t.Parallel()
	runtime := &fakeRuntime{}
	conversations := &fakeConversations{}
	svc, err := chat.New(runtime, &fakeConfigs{}, &fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1"}, "p2": {ID: "p2"}}}, &fakeTokens{tokens: []domain.Token{testToken("key", "a1", time.Now(), "test-only")}}, chat.Options{Conversations: conversations, ContextWindow: 1000, MaxTokens: 100, DefaultConfig: domain.ChatConfig{BaseURL: "https://model.example.com", Model: "model", APIKey: "test-only"}})
	if err != nil {
		t.Fatal(err)
	}
	first, err := svc.NewConversation(t.Context(), "p1", "a1", "first")
	if err != nil {
		t.Fatal(err)
	}
	second, err := svc.NewConversation(t.Context(), "p1", "a1", "second")
	if err != nil {
		t.Fatal(err)
	}
	if specs, _ := runtime.snapshot(); len(specs) != 0 {
		t.Fatal("new conversation started runtime")
	}
	if err := svc.SetConversationArchived(t.Context(), "p1", "a1", first.ID, true); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Send(t.Context(), "p1", "a1", "hello", first.ID); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("archived selection: %v", err)
	}
	if specs, _ := runtime.snapshot(); len(specs) != 0 {
		t.Fatal("archive reached runtime")
	}
	if err := svc.SetConversationArchived(t.Context(), "p1", "a1", first.ID, false); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"", first.ID, second.ID} {
		if _, err := svc.Send(t.Context(), "p1", "a1", "hello", id); err != nil {
			t.Fatal(err)
		}
	}
	specs, _ := runtime.snapshot()
	if len(specs) != 3 {
		t.Fatal(specs)
	}
	if specs[0].SessionID != chat.SessionID("a1", "p1") || specs[1].SessionID == specs[2].SessionID || specs[0].SessionID == specs[1].SessionID {
		t.Fatal("session identity collision")
	}
	if specs[1].ConversationID != first.ID || specs[1].ProjectKey != specs[0].ProjectKey {
		t.Fatal("wrong memory scope")
	}
	wrong := []func() error{
		func() error { _, e := svc.Send(t.Context(), "p1", "a2", "hello", first.ID); return e },
		func() error { _, e := svc.Messages(t.Context(), "p1", "a2", 10, 0, first.ID); return e },
		func() error { _, _, e := svc.Stream(t.Context(), "p1", "a2", first.ID); return e },
		func() error { _, e := svc.Retry(t.Context(), "p1", "a2", first.ID); return e },
		func() error { return svc.Abort(t.Context(), "p1", "a2", first.ID) },
		func() error { _, e := svc.Send(t.Context(), "p1", "a1", "hello", "../../foreign"); return e },
	}
	for i, fn := range wrong {
		if err := fn(); !errors.Is(err, domain.ErrNotFound) {
			t.Fatalf("case%d: %v", i, err)
		}
	}
	if specs, _ := runtime.snapshot(); len(specs) != 3 {
		t.Fatal("rejected selection reached runtime")
	}
}

func (f *fakeConversations) SetChatConversationArchived(_ context.Context, p, a, id string, archived bool) error {
	for _, c := range f.rows {
		if c.TenantID == p && c.AccountID == a && c.ID == id {
			if f.archived == nil {
				f.archived = make(map[string]bool)
			}
			f.archived[id] = archived
			return nil
		}
	}
	return domain.NotFound("test", "对话")
}
