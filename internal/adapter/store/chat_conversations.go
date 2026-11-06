package store

import (
	"context"
	"fmt"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// CreateChatConversation 不存消息，避免出现与pi transcript不同的第二份历史。
func (s *Store) CreateChatConversation(ctx context.Context, c domain.ChatConversation) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO chat_conversations(id,tenant_id,account_id,title,created_at) VALUES(?,?,?,?,?)`, c.ID, c.TenantID, c.AccountID, c.Title, encodeTime(c.CreatedAt))
	if err != nil {
		return fmt.Errorf("store: create chat conversation: %w", err)
	}
	return nil
}

// GetChatConversation 同时约束两项身份，不泄露其他成员是否存在某段对话。
func (s *Store) GetChatConversation(ctx context.Context, project, account, id string) (domain.ChatConversation, error) {
	c := domain.ChatConversation{TenantID: project, AccountID: account}
	var at string
	err := s.read.QueryRowContext(ctx, `SELECT id,title,created_at FROM chat_conversations WHERE tenant_id=? AND account_id=? AND id=? AND archived=0`, project, account, id).Scan(&c.ID, &c.Title, &at)
	if isNotFound(err) {
		return c, notFound("chat_conversation", "对话")
	}
	if err != nil {
		return c, fmt.Errorf("store: get chat conversation: %w", err)
	}
	c.CreatedAt, err = decodeTime(at)
	return c, err
}

// ListChatConversations 以稳定顺序分页，不扫描会话磁盘或拉起模型进程。
func (s *Store) ListChatConversations(ctx context.Context, project, account string, offset int) ([]domain.ChatConversation, int, error) {
	tx, err := s.read.BeginTx(ctx, nil)
	if err != nil {
		return nil, 0, fmt.Errorf("store: list conversations: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	var total int
	if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM chat_conversations WHERE tenant_id=? AND account_id=? AND archived=0`, project, account).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT id,title,created_at FROM chat_conversations WHERE tenant_id=? AND account_id=? AND archived=0 ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET ?`, project, account, offset)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	records := make([]domain.ChatConversation, 0)
	for rows.Next() {
		c := domain.ChatConversation{TenantID: project, AccountID: account}
		var at string
		if err := rows.Scan(&c.ID, &c.Title, &at); err != nil {
			return nil, 0, err
		}
		c.CreatedAt, err = decodeTime(at)
		if err != nil {
			return nil, 0, err
		}
		records = append(records, c)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, err
	}
	return records, total, nil
}

// SetChatConversationArchived 只隐藏入口，正文及其稳定会话身份保留以便恢复。
func (s *Store) SetChatConversationArchived(ctx context.Context, project, account, id string, archived bool) error {
	result, err := s.db.ExecContext(ctx, `UPDATE chat_conversations SET archived=? WHERE tenant_id=? AND account_id=? AND id=?`, archived, project, account, id)
	if err != nil {
		return fmt.Errorf("store: archive chat conversation: %w", err)
	}
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return notFound("chat_conversation", "对话")
	}
	return nil
}

// SetChatConversationTitleIfEmpty 只填充未命名入口，不改变session身份或覆盖显式标题。
func (s *Store) SetChatConversationTitleIfEmpty(ctx context.Context, project, account, id, title string) (bool, error) {
	result, err := s.db.ExecContext(ctx, `UPDATE chat_conversations SET title=? WHERE tenant_id=? AND account_id=? AND id=? AND title=''`, title, project, account, id)
	if err != nil {
		return false, fmt.Errorf("store: save generated title: %w", err)
	}
	n, err := result.RowsAffected()
	return n > 0, err
}
