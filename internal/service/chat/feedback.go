package chat

import (
	"context"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// FeedbackStore 保存回答评价，与会修改记忆的反馈工具分开。
type FeedbackStore interface {
	ChatFeedback(context.Context, string, string, string) (int, error)
	SaveChatFeedback(context.Context, string, string, string, int, time.Time) error
	AnswerFeedback(context.Context, string, string, string) (domain.ChatAnswerFeedback, error)
	SaveAnswerFeedback(context.Context, string, string, string, domain.ChatAnswerFeedback, bool, time.Time) error
}

func validateResponseID(id string) error {
	if len(id) != 64 || strings.Trim(id, "0123456789abcdef") != "" {
		return domain.Invalid("chat_feedback", "请选择要评价的回答")
	}
	return nil
}

// Feedback 的账号由入站身份决定，调用方不能查询其他人的评分。
func (s *Service) Feedback(ctx context.Context, project, account, response string) (int, error) {
	if err := validateResponseID(response); err != nil {
		return 0, err
	}
	if s.opts.Feedback == nil {
		return 0, domain.Wrap(domain.ErrChatUnavailable, "chat_feedback", "反馈暂时不可用", nil)
	}
	return s.opts.Feedback.ChatFeedback(ctx, project, account, response)
}

// RateAnswer 不读取或启动对话进程，评分不应产生新的模型调用。
func (s *Service) RateAnswer(ctx context.Context, project, account, response string, vote int) error {
	if err := validateResponseID(response); err != nil {
		return err
	}
	if vote < -1 || vote > 1 {
		return domain.Invalid("chat_feedback", "请选择赞同或不赞同")
	}
	if s.opts.Feedback == nil {
		return domain.Wrap(domain.ErrChatUnavailable, "chat_feedback", "反馈暂时不可用", nil)
	}
	return s.opts.Feedback.SaveChatFeedback(ctx, project, account, response, vote, s.now().UTC())
}

// AnswerFeedback 返回独立持久化的意见，不启动Pi。
func (s *Service) AnswerFeedback(ctx context.Context, project, account, response string) (domain.ChatAnswerFeedback, error) {
	if err := validateResponseID(response); err != nil {
		return domain.ChatAnswerFeedback{}, err
	}
	if s.opts.Feedback == nil {
		return domain.ChatAnswerFeedback{}, domain.Wrap(domain.ErrChatUnavailable, "chat_feedback", "反馈暂时不可用", nil)
	}
	return s.opts.Feedback.AnswerFeedback(ctx, project, account, response)
}

// SaveAnswerFeedback 校验意见长度和对话归属后一次写入评分及意见。
func (s *Service) SaveAnswerFeedback(ctx context.Context, project, account, response string, f domain.ChatAnswerFeedback, details bool) error {
	if err := validateResponseID(response); err != nil {
		return err
	}
	if f.Vote < -1 || f.Vote > 1 {
		return domain.Invalid("chat_feedback", "请选择赞同或不赞同")
	}
	if utf8.RuneCountInString(f.Comment) > 2000 || utf8.RuneCountInString(f.AnswerExcerpt) > 2000 {
		return domain.Invalid("chat_feedback", "意见和回答摘录请控制在2000字以内")
	}
	if f.ConversationID != "" {
		if s.opts.Conversations == nil {
			return domain.NotFound("chat_feedback", "对话")
		}
		if _, err := s.opts.Conversations.GetChatConversation(ctx, project, normalizeAccountID(account), f.ConversationID); err != nil {
			return err
		}
	}
	if s.opts.Feedback == nil {
		return domain.Wrap(domain.ErrChatUnavailable, "chat_feedback", "反馈暂时不可用", nil)
	}
	return s.opts.Feedback.SaveAnswerFeedback(ctx, project, account, response, f, details, s.now().UTC())
}
