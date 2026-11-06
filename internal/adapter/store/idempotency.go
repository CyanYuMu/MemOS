package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ClaimIdempotency 尝试占用 (tenant_id, key)。
// claimed=true 表示本次首次占用；false 时返回已有记录供调用方判断重放或冲突。
func (s *Store) ClaimIdempotency(
	ctx context.Context,
	rec domain.IdempotencyRecord,
) (domain.IdempotencyRecord, bool, error) {
	res, err := s.db.ExecContext(ctx, `
		INSERT INTO idempotency_keys (tenant_id, idem_key, tool, request_hash, state, response, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, '', ?, ?)
		ON CONFLICT (tenant_id, idem_key) DO NOTHING`,
		rec.TenantID, rec.Key, rec.Tool, rec.RequestHash, rec.State,
		encodeTime(rec.CreatedAt), encodeTime(rec.UpdatedAt))
	if err != nil {
		return domain.IdempotencyRecord{}, false, fmt.Errorf("store: claim idempotency: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return domain.IdempotencyRecord{}, false, fmt.Errorf("store: claim idempotency rows: %w", err)
	}
	if n == 1 {
		return rec, true, nil
	}

	var (
		existing             domain.IdempotencyRecord
		response             string
		createdAt, updatedAt string
	)
	err = s.db.QueryRowContext(ctx, `
		SELECT tenant_id, idem_key, tool, request_hash, state, response, created_at, updated_at
		FROM idempotency_keys WHERE tenant_id = ? AND idem_key = ?`,
		rec.TenantID, rec.Key).Scan(
		&existing.TenantID, &existing.Key, &existing.Tool, &existing.RequestHash,
		&existing.State, &response, &createdAt, &updatedAt)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return domain.IdempotencyRecord{}, false, errors.New("store: idempotency row disappeared")
		}
		return domain.IdempotencyRecord{}, false, fmt.Errorf("store: load idempotency: %w", err)
	}
	existing.Response = []byte(response)
	if existing.CreatedAt, err = decodeTime(createdAt); err != nil {
		return domain.IdempotencyRecord{}, false, err
	}
	if existing.UpdatedAt, err = decodeTime(updatedAt); err != nil {
		return domain.IdempotencyRecord{}, false, err
	}
	return existing, false, nil
}

// CompleteIdempotency 把占用中的记录置为 succeeded 并保存响应。
func (s *Store) CompleteIdempotency(ctx context.Context, p domain.IdempotencyOutcome) error {
	return s.transitionIdempotency(ctx, idempotencyTransition{
		Op:       "complete_idempotency",
		TenantID: p.TenantID,
		Key:      p.Key,
		From:     domain.IdempotencyInProgress,
		To:       domain.IdempotencySucceeded,
		Response: p.Response,
		At:       p.At,
	})
}

// AcceptIdempotency 把异步入队成功的记录置为 accepted，并保存“已接收”响应。
func (s *Store) AcceptIdempotency(ctx context.Context, p domain.IdempotencyOutcome) error {
	return s.transitionIdempotency(ctx, idempotencyTransition{
		Op:       "accept_idempotency",
		TenantID: p.TenantID,
		Key:      p.Key,
		From:     domain.IdempotencyInProgress,
		To:       domain.IdempotencyAccepted,
		Response: p.Response,
		At:       p.At,
	})
}

// SucceedIdempotency 把 worker 已写成功的 accepted 记录置为 succeeded，保留原响应。
func (s *Store) SucceedIdempotency(ctx context.Context, tenantID, key string, at time.Time) error {
	return s.transitionIdempotency(ctx, idempotencyTransition{
		Op:       "succeed_idempotency",
		TenantID: tenantID,
		Key:      key,
		From:     domain.IdempotencyAccepted,
		To:       domain.IdempotencySucceeded,
		At:       at,
	})
}

// idempotencyTransition 是状态收敛的入参。
type idempotencyTransition struct {
	Op       string
	TenantID string
	Key      string
	From     string
	To       string
	Response []byte
	At       time.Time
}

// transitionIdempotency 只在当前状态匹配时更新，避免并发下覆盖别人的记录。
func (s *Store) transitionIdempotency(ctx context.Context, tr idempotencyTransition) error {
	query := `UPDATE idempotency_keys SET state = ?, updated_at = ? WHERE tenant_id = ? AND idem_key = ? AND state = ?`
	args := []any{tr.To, encodeTime(tr.At), tr.TenantID, tr.Key, tr.From}
	if tr.Response != nil {
		query = `UPDATE idempotency_keys SET state = ?, response = ?, updated_at = ? WHERE tenant_id = ? AND idem_key = ? AND state = ?`
		args = []any{tr.To, string(tr.Response), encodeTime(tr.At), tr.TenantID, tr.Key, tr.From}
	}
	res, err := s.db.ExecContext(ctx, query, args...)
	if err != nil {
		return fmt.Errorf("store: %s: %w", tr.Op, err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: %s rows: %w", tr.Op, err)
	}
	if n == 0 {
		return notFound(tr.Op, "幂等记录")
	}
	return nil
}

// ReleaseIdempotency 删除尚未收敛的记录（in_progress/accepted），让客户端可以重试。
func (s *Store) ReleaseIdempotency(ctx context.Context, tenantID, key string) error {
	if _, err := s.db.ExecContext(ctx,
		`DELETE FROM idempotency_keys WHERE tenant_id = ? AND idem_key = ? AND state IN (?, ?)`,
		tenantID, key, domain.IdempotencyInProgress, domain.IdempotencyAccepted); err != nil {
		return fmt.Errorf("store: release idempotency: %w", err)
	}
	return nil
}

// PurgeIdempotencyBefore 删除 updated_at 早于 before 的记录，返回删除条数。
func (s *Store) PurgeIdempotencyBefore(ctx context.Context, before time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx, `DELETE FROM idempotency_keys WHERE updated_at < ?`, encodeTime(before))
	if err != nil {
		return 0, fmt.Errorf("store: purge idempotency: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: purge idempotency rows: %w", err)
	}
	return n, nil
}
