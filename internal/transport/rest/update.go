package rest

import (
	"context"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/response"
)

func (s *Server) handleUpdate(ctx context.Context, caller domain.Caller, body map[string]any, _ string) (toolResult, error) {
	in := domain.MemoryUpdate{ID: httpx.String(body["memory_id"]), Content: httpx.String(body["content"])}
	if title, ok := body["title"].(string); ok {
		in.Title = &title
	}
	result, err := s.deps.Memory.Update(ctx, caller, in)
	if err != nil {
		return toolResult{}, err
	}
	return toolResult{payload: response.Update(result)}, nil
}
