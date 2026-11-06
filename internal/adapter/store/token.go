package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// CreateToken 写入 Token 元数据。hash 唯一，重复时返回 ErrConflict。
func (s *Store) CreateToken(ctx context.Context, t domain.Token) error {
	if err := t.Validate(); err != nil {
		return err
	}
	created := t.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO tokens (id, kind, tenant_id, user_id, hash, prefix, raw, label, created_at, last_used_at, revoked_at, created_by_account_id, expires_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		t.ID, string(t.Kind), t.TenantID, t.UserID, t.Hash[:], t.Prefix, t.Raw, t.Label,
		encodeTime(created), optionalTimeText(t.LastUsedAt), optionalTimeText(t.RevokedAt), t.CreatedByAccountID,
		optionalTimeText(t.ExpiresAt))
	if err != nil {
		if isUniqueViolation(err) {
			return conflict("create_token", "Token 已存在", err)
		}
		return fmt.Errorf("store: create token: %w", err)
	}
	return nil
}

// UpsertToken 用启动配置覆盖同 ID 的静态 Token：同步改绑后的
// 租户/用户，但保留 revoked_at 与 expires_at —— 管理员吊销过的静态 Token
// 不能因重启复活。迁移导入的展示名（非空且不是启动配置的默认名）同样保留，
// 否则一次性迁移的 Key 名称会在下次启动时被启动配置覆盖掉。
//
// 静态 Token 的 ID 由明文哈希派生（`static-` + hash[:6]），同一明文永远落在同一行，
// 所以这里不需要处理“同一哈希换了 ID”的情况。
func (s *Store) UpsertToken(ctx context.Context, t domain.Token) error {
	if err := t.Validate(); err != nil {
		return err
	}
	created := t.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	if _, err := s.db.ExecContext(ctx, `
		INSERT INTO tokens (id, kind, tenant_id, user_id, hash, prefix, raw, label, created_at, last_used_at, revoked_at, expires_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
		ON CONFLICT(id) DO UPDATE SET
			kind = excluded.kind,
			tenant_id = excluded.tenant_id,
			user_id = excluded.user_id,
			hash = excluded.hash,
			prefix = excluded.prefix,
			label = CASE WHEN tokens.label = '' THEN excluded.label ELSE tokens.label END,
			raw = CASE WHEN excluded.raw <> '' THEN excluded.raw ELSE tokens.raw END`,
		t.ID, string(t.Kind), t.TenantID, t.UserID, t.Hash[:], t.Prefix, t.Raw, t.Label,
		encodeTime(created), optionalTimeText(t.LastUsedAt), optionalTimeText(t.ExpiresAt)); err != nil {
		if isUniqueViolation(err) {
			// 同一个明文哈希已经挂在另一个 ID 上：只能是人手改过库。
			return conflict("upsert_token", "Token 哈希冲突", err)
		}
		return fmt.Errorf("store: upsert token: %w", err)
	}
	return nil
}

// TokenByHash 按哈希查找 Token。
func (s *Store) TokenByHash(ctx context.Context, hash [32]byte) (domain.Token, error) {
	row := s.read.QueryRowContext(ctx, `
		SELECT id, kind, tenant_id, user_id, hash, prefix, raw, label, created_at, last_used_at, revoked_at, created_by_account_id, expires_at
		FROM tokens WHERE hash = ?`, hash[:])
	t, err := scanToken(row)
	if err != nil {
		if isNotFound(err) {
			return domain.Token{}, notFound("token_by_hash", "Token")
		}
		return domain.Token{}, fmt.Errorf("store: token by hash: %w", err)
	}
	return t, nil
}

// GetToken 按 ID 查找 Token。
func (s *Store) GetToken(ctx context.Context, id string) (domain.Token, error) {
	row := s.read.QueryRowContext(ctx, `
		SELECT id, kind, tenant_id, user_id, hash, prefix, raw, label, created_at, last_used_at, revoked_at, created_by_account_id, expires_at
		FROM tokens WHERE id = ?`, id)
	t, err := scanToken(row)
	if err != nil {
		if isNotFound(err) {
			return domain.Token{}, notFound("get_token", "Token")
		}
		return domain.Token{}, fmt.Errorf("store: get token: %w", err)
	}
	return t, nil
}

// ListTokens 列出 Token；tenantID 为空时返回全部。
func (s *Store) ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error) {
	query := `SELECT id, kind, tenant_id, user_id, hash, prefix, raw, label, created_at, last_used_at, revoked_at, created_by_account_id, expires_at FROM tokens`
	var args []any
	if tenantID != "" {
		query += ` WHERE tenant_id = ?`
		args = append(args, tenantID)
	}
	query += ` ORDER BY created_at DESC, id DESC`

	rows, err := s.read.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("store: list tokens: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []domain.Token
	for rows.Next() {
		t, err := scanToken(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan token: %w", err)
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// RevokeToken 设置吊销时间；Token 不存在返回 ErrNotFound。
func (s *Store) RevokeToken(ctx context.Context, id string, at time.Time) error {
	res, err := s.db.ExecContext(ctx, `UPDATE tokens SET revoked_at = ? WHERE id = ?`, encodeTime(at), id)
	if err != nil {
		return fmt.Errorf("store: revoke token: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: revoke token rows: %w", err)
	}
	if n == 0 {
		return notFound("revoke_token", "Token")
	}
	return nil
}

// UpdateTokenLabel 修改 Token 的展示名。已吊销的 Key 也允许改名（列表不展示，
// 但管理端仍可能按历史记录检索）；Token 不存在返回 ErrNotFound。
func (s *Store) UpdateTokenLabel(ctx context.Context, id, label string) error {
	res, err := s.db.ExecContext(ctx, `UPDATE tokens SET label = ? WHERE id = ?`, label, id)
	if err != nil {
		return fmt.Errorf("store: update token label: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: update token label rows: %w", err)
	}
	if n == 0 {
		return notFound("update_token_label", "Token")
	}
	return nil
}

// TouchToken 更新最后使用时间。
func (s *Store) TouchToken(ctx context.Context, id string, at time.Time) error {
	_, err := s.db.ExecContext(ctx, `UPDATE tokens SET last_used_at = ? WHERE id = ?`, encodeTime(at), id)
	if err != nil {
		return fmt.Errorf("store: touch token: %w", err)
	}
	return nil
}

func scanToken(row scanner) (domain.Token, error) {
	var (
		t                   domain.Token
		kind, created       string
		hash                []byte
		lastUsed, revokedAt string
		expiresAt           string
	)
	if err := row.Scan(&t.ID, &kind, &t.TenantID, &t.UserID, &hash, &t.Prefix, &t.Raw, &t.Label,
		&created, &lastUsed, &revokedAt, &t.CreatedByAccountID, &expiresAt); err != nil {
		return domain.Token{}, err
	}
	if len(hash) != len(t.Hash) {
		return domain.Token{}, errors.New("store: token hash has unexpected length")
	}
	copy(t.Hash[:], hash)
	t.Kind = domain.TokenKind(kind)

	parsed, err := decodeTime(created)
	if err != nil {
		return domain.Token{}, err
	}
	t.CreatedAt = parsed
	if err := setOptionalTime(&t.LastUsedAt, lastUsed); err != nil {
		return domain.Token{}, err
	}
	if err := setOptionalTime(&t.RevokedAt, revokedAt); err != nil {
		return domain.Token{}, err
	}
	if err := setOptionalTime(&t.ExpiresAt, expiresAt); err != nil {
		return domain.Token{}, err
	}
	return t, nil
}
