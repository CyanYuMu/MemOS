package store

import (
	"context"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ChatFeedback 只读当前账号的评分，不把其他成员的喜好合并到回答上。
func (s *Store) ChatFeedback(ctx context.Context, tenant, account, response string) (int, error) {
	var vote int
	err := s.read.QueryRowContext(ctx, `SELECT vote FROM chat_feedback WHERE tenant_id=? AND account_id=? AND response_id=?`, tenant, account, response).Scan(&vote)
	if isNotFound(err) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("store: read chat feedback: %w", err)
	}
	return vote, nil
}

// SaveChatFeedback 的0是取消反馈，更新同一行可防止重复点击累计评分。
func (s *Store) SaveChatFeedback(ctx context.Context, tenant, account, response string, vote int, at time.Time) error {
	return s.SaveAnswerFeedback(ctx, tenant, account, response, domain.ChatAnswerFeedback{Vote: vote}, false, at)
}

// AnswerFeedback 同时约束项目和账号，意见不经模型历史回推。
func (s *Store) AnswerFeedback(ctx context.Context, tenant, account, response string) (domain.ChatAnswerFeedback, error) {
	var f domain.ChatAnswerFeedback
	err := s.read.QueryRowContext(ctx, `SELECT vote,comment,conversation_id,answer_excerpt,updated_at FROM chat_feedback WHERE tenant_id=? AND account_id=? AND response_id=?`, tenant, account, response).Scan(&f.Vote, &f.Comment, &f.ConversationID, &f.AnswerExcerpt, &f.UpdatedAt)
	if isNotFound(err) {
		return f, nil
	}
	if err != nil {
		return f, fmt.Errorf("store: read answer feedback: %w", err)
	}
	return f, nil
}

// SaveAnswerFeedback 在同一条记录原子保存；单独切换评分保留原意见。
func (s *Store) SaveAnswerFeedback(ctx context.Context, tenant, account, response string, f domain.ChatAnswerFeedback, details bool, at time.Time) error {
	if f.Vote == 0 {
		_, err := s.db.ExecContext(ctx, `DELETE FROM chat_feedback WHERE tenant_id=? AND account_id=? AND response_id=?`, tenant, account, response)
		return err
	}
	_, err := s.db.ExecContext(ctx, `INSERT INTO chat_feedback(tenant_id,account_id,response_id,vote,updated_at,comment,conversation_id,answer_excerpt) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,account_id,response_id) DO UPDATE SET vote=excluded.vote,updated_at=excluded.updated_at,comment=CASE WHEN ? THEN excluded.comment ELSE chat_feedback.comment END,conversation_id=CASE WHEN ? THEN excluded.conversation_id ELSE chat_feedback.conversation_id END,answer_excerpt=CASE WHEN ? THEN excluded.answer_excerpt ELSE chat_feedback.answer_excerpt END`, tenant, account, response, f.Vote, encodeTime(at), f.Comment, f.ConversationID, f.AnswerExcerpt, details, details, details)
	if err != nil {
		return fmt.Errorf("store: save answer feedback: %w", err)
	}
	return nil
}
