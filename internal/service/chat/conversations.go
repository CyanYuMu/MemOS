package chat

import (
	"context"
	"strings"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ConversationStore 的读取必须在存储层以项目与账号共同限定。
type ConversationStore interface {
	CreateChatConversation(context.Context, domain.ChatConversation) error
	GetChatConversation(context.Context, string, string, string) (domain.ChatConversation, error)
	ListChatConversations(context.Context, string, string, int) ([]domain.ChatConversation, int, error)
	SetChatConversationArchived(context.Context, string, string, string, bool) error
}

// SetConversationArchived 不经过模型或会话正文，恢复时沿用相同ID。
func (s *Service) SetConversationArchived(ctx context.Context, project, account, id string, archived bool) error {
	if id == "" || len(id) > 80 {
		return domain.NotFound(op, "对话")
	}
	if s.opts.Conversations == nil {
		return domain.Wrap(domain.ErrChatUnavailable, op, "对话历史暂时不可用，请稍后重试", nil)
	}
	return s.opts.Conversations.SetChatConversationArchived(ctx, project, normalizeAccountID(account), id, archived)
}

// Conversations 不启动pi，旧固定会话由前端单列入口保留。
func (s *Service) Conversations(ctx context.Context, project, account string, offset int) (any, error) {
	if offset < 0 {
		return nil, domain.Invalid(op, "请选择有效的历史页")
	}
	if s.opts.Conversations == nil {
		return nil, domain.Wrap(domain.ErrChatUnavailable, op, "对话历史暂时不可用，请稍后重试", nil)
	}
	rows, total, err := s.opts.Conversations.ListChatConversations(ctx, project, normalizeAccountID(account), offset)
	if err != nil {
		return nil, err
	}
	return struct {
		Records []domain.ChatConversation `json:"records"`
		Total   int                       `json:"total"`
	}{rows, total}, nil
}

// NewConversation 只创建独立入口，不清空旧记录也不自动发送介绍。
func (s *Service) NewConversation(ctx context.Context, project, account, title string) (domain.ChatConversation, error) {
	if s.opts.Conversations == nil {
		return domain.ChatConversation{}, domain.Wrap(domain.ErrChatUnavailable, op, "新建对话暂时不可用，请稍后重试", nil)
	}
	if _, err := s.ready(ctx, project, account, ""); err != nil {
		return domain.ChatConversation{}, err
	}
	title = strings.TrimSpace(title)
	if utf8.RuneCountInString(title) > 80 {
		return domain.ChatConversation{}, domain.Invalid(op, "对话标题请控制在80字以内")
	}
	c := domain.ChatConversation{ID: domain.NewID(), TenantID: project, AccountID: normalizeAccountID(account), Title: title, CreatedAt: s.now().UTC()}
	if err := s.opts.Conversations.CreateChatConversation(ctx, c); err != nil {
		return domain.ChatConversation{}, err
	}
	return c, nil
}

func (s *Service) selectConversation(ctx context.Context, spec domain.ChatSessionSpec, id string) (domain.ChatSessionSpec, error) {
	if id == "" {
		return spec, nil
	}
	if len(id) > 80 || s.opts.Conversations == nil {
		return domain.ChatSessionSpec{}, domain.NotFound(op, "对话")
	}
	c, err := s.opts.Conversations.GetChatConversation(ctx, spec.TenantID, spec.AccountID, id)
	if err != nil {
		return domain.ChatSessionSpec{}, err
	}
	spec.ConversationID = c.ID
	spec.SessionID = SessionID(spec.AccountID, spec.TenantID+"|conversation:"+c.ID)
	return spec, nil
}
