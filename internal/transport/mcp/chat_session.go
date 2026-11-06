package mcp

import (
	"context"
	"strings"
)

type chatSessionKey struct{}

// withChatSession 仅接收runtime格式的关联标识，绝不参与身份和权限判断。
func withChatSession(ctx context.Context, id string) context.Context {
	if len(id) != 29 || !strings.HasPrefix(id, "chat-") || strings.Trim(id[5:], "0123456789abcdef") != "" {
		return ctx
	}
	return context.WithValue(ctx, chatSessionKey{}, id)
}

func sessionIDFor(ctx context.Context, explicit string) string {
	if id, ok := ctx.Value(chatSessionKey{}).(string); ok {
		return id
	}
	return explicit
}
