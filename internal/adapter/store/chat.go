package store

import (
	"context"
	"fmt"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// GetChatConfig 读取项目的 BYOK 配置。未配置返回 ErrNotFound，由调用方
// 转成 configured=false。
func (s *Store) GetChatConfig(ctx context.Context, tenantID string) (domain.ChatConfig, error) {
	var (
		cfg     domain.ChatConfig
		updated string
	)
	err := s.read.QueryRowContext(ctx, `
		SELECT tenant_id, base_url, model, api_key, updated_at
		FROM chat_configs WHERE tenant_id = ?`, tenantID).
		Scan(&cfg.TenantID, &cfg.BaseURL, &cfg.Model, &cfg.APIKey, &updated)
	if err != nil {
		if isNotFound(err) {
			return domain.ChatConfig{}, notFound("get_chat_config", "聊天配置")
		}
		return domain.ChatConfig{}, fmt.Errorf("store: get chat config: %w", err)
	}
	parsed, err := decodeTime(updated)
	if err != nil {
		return domain.ChatConfig{}, fmt.Errorf("store: decode chat config time: %w", err)
	}
	cfg.UpdatedAt = parsed
	return cfg, nil
}

// UpsertChatConfig 写入或覆盖项目的 BYOK 配置。
func (s *Store) UpsertChatConfig(ctx context.Context, cfg domain.ChatConfig) error {
	if cfg.TenantID == "" {
		return domain.Invalid("upsert_chat_config", "tenant id 不能为空")
	}
	if _, err := s.db.ExecContext(ctx, `
		INSERT INTO chat_configs (tenant_id, base_url, model, api_key, updated_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(tenant_id) DO UPDATE SET
			base_url = excluded.base_url,
			model = excluded.model,
			api_key = excluded.api_key,
			updated_at = excluded.updated_at`,
		cfg.TenantID, cfg.BaseURL, cfg.Model, cfg.APIKey, encodeTime(cfg.UpdatedAt)); err != nil {
		return fmt.Errorf("store: upsert chat config: %w", err)
	}
	return nil
}
