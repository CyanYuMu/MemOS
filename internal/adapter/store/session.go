package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const sessionColumns = `id, hash, account_id, current_tenant_id, created_at, expires_at, last_seen_at, revoked_at`

// CreateSession 写入会话；hash 唯一。
func (s *Store) CreateSession(ctx context.Context, sess domain.Session) error {
	created := sess.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO sessions (`+sessionColumns+`)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		sess.ID, sess.Hash[:], sess.AccountID, sess.CurrentTenantID,
		encodeTime(created), encodeTime(sess.ExpiresAt),
		optionalTimeText(sess.LastSeenAt), optionalTimeText(sess.RevokedAt))
	if err != nil {
		if isUniqueViolation(err) {
			return conflict("create_session", "会话已存在", err)
		}
		return fmt.Errorf("store: create session: %w", err)
	}
	return nil
}

// SessionByHash 按会话 Token 的哈希读取会话。
func (s *Store) SessionByHash(ctx context.Context, hash [32]byte) (domain.Session, error) {
	sess, err := scanSession(s.read.QueryRowContext(ctx,
		`SELECT `+sessionColumns+` FROM sessions WHERE hash = ?`, hash[:]))
	if err != nil {
		if isNotFound(err) {
			return domain.Session{}, notFound("session_by_hash", "会话")
		}
		return domain.Session{}, fmt.Errorf("store: session by hash: %w", err)
	}
	return sess, nil
}

// SetSessionTenant 修改会话的当前项目空间。
func (s *Store) SetSessionTenant(ctx context.Context, id, tenantID string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET current_tenant_id = ? WHERE id = ?`, tenantID, id)
	if err != nil {
		return fmt.Errorf("store: set session tenant: %w", err)
	}
	return requireAffected(res, "set_session_tenant", "会话")
}

// GetSession 按 ID 读取会话。
func (s *Store) GetSession(ctx context.Context, id string) (domain.Session, error) {
	sess, err := scanSession(s.read.QueryRowContext(ctx,
		`SELECT `+sessionColumns+` FROM sessions WHERE id = ?`, id))
	if err != nil {
		if isNotFound(err) {
			return domain.Session{}, notFound("get_session", "会话")
		}
		return domain.Session{}, fmt.Errorf("store: get session: %w", err)
	}
	return sess, nil
}

// ExtendSession 同时更新最近访问时间与滑动后的过期时间。
// 条件里的 revoked_at/expires_at 保证并发吊销或已过期时不会被这条 UPDATE 复活。
func (s *Store) ExtendSession(ctx context.Context, id string, lastSeen, expiresAt time.Time) error {
	if _, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET last_seen_at = ?, expires_at = ?
		 WHERE id = ? AND revoked_at = '' AND expires_at > ?`,
		encodeTime(lastSeen), encodeTime(expiresAt), id, encodeTime(lastSeen)); err != nil {
		return fmt.Errorf("store: extend session: %w", err)
	}
	return nil
}

// RevokeSession 吊销会话；已吊销的会话保留原吊销时间。
func (s *Store) RevokeSession(ctx context.Context, id string, at time.Time) error {
	if _, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at = ''`, encodeTime(at), id); err != nil {
		return fmt.Errorf("store: revoke session: %w", err)
	}
	return nil
}

// RevokeAccountSessions 吊销账号名下全部未吊销会话。
func (s *Store) RevokeAccountSessions(ctx context.Context, accountID string, at time.Time) error {
	if _, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at = ''`,
		encodeTime(at), accountID); err != nil {
		return fmt.Errorf("store: revoke account sessions: %w", err)
	}
	return nil
}

// PurgeSessionsBefore 删除过期时间早于 before 的会话，返回删除行数。
func (s *Store) PurgeSessionsBefore(ctx context.Context, before time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx, `DELETE FROM sessions WHERE expires_at < ?`, encodeTime(before))
	if err != nil {
		return 0, fmt.Errorf("store: purge sessions: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: purge sessions rows: %w", err)
	}
	return n, nil
}

func scanSession(row scanner) (domain.Session, error) {
	var (
		sess              domain.Session
		hash              []byte
		created, expires  string
		lastSeen, revoked string
	)
	if err := row.Scan(&sess.ID, &hash, &sess.AccountID, &sess.CurrentTenantID,
		&created, &expires, &lastSeen, &revoked); err != nil {
		return domain.Session{}, err
	}
	if len(hash) != len(sess.Hash) {
		return domain.Session{}, errors.New("store: session hash has unexpected length")
	}
	copy(sess.Hash[:], hash)
	var err error
	if sess.CreatedAt, err = decodeTime(created); err != nil {
		return domain.Session{}, fmt.Errorf("session created_at: %w", err)
	}
	if sess.ExpiresAt, err = decodeTime(expires); err != nil {
		return domain.Session{}, fmt.Errorf("session expires_at: %w", err)
	}
	if err = setOptionalTime(&sess.LastSeenAt, lastSeen); err != nil {
		return domain.Session{}, fmt.Errorf("session last_seen_at: %w", err)
	}
	if err = setOptionalTime(&sess.RevokedAt, revoked); err != nil {
		return domain.Session{}, fmt.Errorf("session revoked_at: %w", err)
	}
	return sess, nil
}

// ListSessions 返回账号名下未吊销的会话（含已过期），按创建时间倒序。
func (s *Store) ListSessions(ctx context.Context, accountID string) ([]domain.Session, error) {
	rows, err := s.read.QueryContext(ctx,
		`SELECT `+sessionColumns+` FROM sessions WHERE account_id = ? AND revoked_at = ''
		 ORDER BY created_at DESC`, accountID)
	if err != nil {
		return nil, fmt.Errorf("store: list sessions: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []domain.Session
	for rows.Next() {
		sess, err := scanSession(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan session: %w", err)
		}
		out = append(out, sess)
	}
	return out, rows.Err()
}

// RevokeAccountSessionsExcept 吊销账号名下除 keepID 外的未过期、未吊销会话，返回吊销条数。
func (s *Store) RevokeAccountSessionsExcept(
	ctx context.Context,
	accountID,
	keepID string,
	at time.Time,
) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET revoked_at = ? WHERE account_id = ? AND id <> ? AND revoked_at = '' AND expires_at > ?`,
		encodeTime(at), accountID, keepID, encodeTime(at))
	if err != nil {
		return 0, fmt.Errorf("store: revoke other sessions: %w", err)
	}
	return res.RowsAffected()
}
