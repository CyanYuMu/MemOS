package chat

import (
	"context"
	"net/http"
	"strconv"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

func (s *Server) conversations(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any, project string) (any, error) {
	offset := 0
	if raw := r.URL.Query().Get("offset"); raw != "" {
		var err error
		offset, err = strconv.Atoi(raw)
		if err != nil {
			return nil, domain.Invalid("chat", "请选择有效的历史页")
		}
	}
	return s.deps.Chat.Conversations(ctx, project, p.AccountID, offset)
}

func (s *Server) newConversation(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any, project string) (any, error) {
	return s.deps.Chat.NewConversation(ctx, project, p.AccountID, httpx.String(body["title"]))
}

func (s *Server) archiveConversation(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any, project string) (any, error) {
	archived, ok := body["archived"].(bool)
	if !ok {
		return nil, domain.Invalid("chat", "请选择归档或恢复对话")
	}
	if err := s.deps.Chat.SetConversationArchived(ctx, project, p.AccountID, r.PathValue("conversationID"), archived); err != nil {
		return nil, err
	}
	return map[string]any{"id": r.PathValue("conversationID"), "archived": archived}, nil
}
