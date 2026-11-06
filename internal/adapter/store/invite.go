package store

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const inviteColumns = `id, tenant_id, role, token_hash, created_by_account_id, created_at,
	expires_at, used_at, used_by_account_id, revoked_at`

// CreateInvite 写入一条邀请；token 哈希唯一，冲突映射 ErrConflict。
func (s *Store) CreateInvite(ctx context.Context, inv domain.ProjectInvite) error {
	if inv.ID == "" || inv.TenantID == "" || !inv.Role.Valid() {
		return domain.Invalid("create_invite", "邀请 id、项目与角色不能为空")
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO project_invites (`+inviteColumns+`)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		inv.ID, inv.TenantID, string(inv.Role), inv.TokenHash[:], inv.CreatedByAccountID,
		encodeTime(inv.CreatedAt), encodeTime(inv.ExpiresAt), optionalTimeText(inv.UsedAt),
		inv.UsedByAccountID, optionalTimeText(inv.RevokedAt))
	if err != nil {
		if isUniqueViolation(err) {
			return domain.Wrap(domain.ErrConflict, "create_invite", "邀请已存在", err)
		}
		return fmt.Errorf("store: create invite: %w", err)
	}
	return nil
}

// GetInviteByHash 按 token 的 SHA-256 查邀请；不存在返回 ErrNotFound。
func (s *Store) GetInviteByHash(ctx context.Context, hash [32]byte) (domain.ProjectInvite, error) {
	return s.getInvite(ctx, s.read, `SELECT `+inviteColumns+` FROM project_invites WHERE token_hash = ?`, hash[:])
}

// GetInvite 按邀请 ID 读取。
func (s *Store) GetInvite(ctx context.Context, id string) (domain.ProjectInvite, error) {
	return s.getInvite(ctx, s.read, `SELECT `+inviteColumns+` FROM project_invites WHERE id = ?`, id)
}

// queryer 让同一段读取逻辑同时用于只读连接池与事务。
type queryer interface {
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

func (s *Store) getInvite(ctx context.Context, q queryer, query string, arg any) (domain.ProjectInvite, error) {
	row := q.QueryRowContext(ctx, query, arg)
	inv, err := scanInvite(row)
	if err != nil {
		if isNotFound(err) {
			return domain.ProjectInvite{}, notFound("get_invite", "邀请")
		}
		return domain.ProjectInvite{}, fmt.Errorf("store: get invite: %w", err)
	}
	return inv, nil
}

// ListInvites 按创建时间倒序返回项目的邀请。
func (s *Store) ListInvites(ctx context.Context, tenantID string) ([]domain.ProjectInvite, error) {
	rows, err := s.read.QueryContext(ctx,
		`SELECT `+inviteColumns+` FROM project_invites WHERE tenant_id = ? ORDER BY created_at DESC`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("store: list invites: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []domain.ProjectInvite
	for rows.Next() {
		inv, err := scanInvite(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan invite: %w", err)
		}
		out = append(out, inv)
	}
	return out, rows.Err()
}

// RevokeInvite 标记撤销；已撤销的行不重复写，已使用的行不会被改写。
func (s *Store) RevokeInvite(ctx context.Context, tenantID, id string, at time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE project_invites SET revoked_at = ?
		WHERE id = ? AND tenant_id = ?
		  AND revoked_at = ''
		  AND used_at = ''`,
		encodeTime(at), id, tenantID)
	if err != nil {
		return fmt.Errorf("store: revoke invite: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: revoke invite rows: %w", err)
	}
	if n == 0 {
		return notFound("revoke_invite", "邀请")
	}
	return nil
}

// UpdateInviteRole 修改尚未使用的邀请的角色。已使用或已撤销的行不改写，
// 过期判断在服务层（避免 store 里再引入一个时钟）。
func (s *Store) UpdateInviteRole(ctx context.Context, tenantID, id string, role domain.Role) error {
	if !role.Valid() {
		return domain.Invalid("update_invite_role", "角色必须是 viewer、editor 或 owner")
	}
	res, err := s.db.ExecContext(ctx, `
		UPDATE project_invites SET role = ?
		WHERE id = ? AND tenant_id = ?
		  AND revoked_at = ''
		  AND used_at = ''`,
		string(role), id, tenantID)
	if err != nil {
		return fmt.Errorf("store: update invite role: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: update invite role rows: %w", err)
	}
	if n == 0 {
		return notFound("update_invite_role", "邀请")
	}
	return nil
}

// RedeemInvite 在一个事务里抢占邀请、写/升级成员关系并切换当前项目。
// 事务内先读后写：写连接只有一个，读到的状态不会被另一个写事务插入修改。
func (s *Store) RedeemInvite(ctx context.Context, in domain.RedeemInviteInput) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: redeem invite: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	inv, err := s.getInvite(ctx, tx, `SELECT `+inviteColumns+` FROM project_invites WHERE id = ?`, in.InviteID)
	if err != nil {
		return err
	}
	if inv.TenantID != in.TenantID || inv.Status(in.Now) != domain.InviteActive {
		return notFound("redeem_invite", "邀请")
	}
	res, err := tx.ExecContext(ctx, `
		UPDATE project_invites SET used_at = ?, used_by_account_id = ?
		WHERE id = ?
		  AND used_at = ''
		  AND revoked_at = ''`,
		encodeTime(in.Now), in.AccountID, in.InviteID)
	if err != nil {
		return fmt.Errorf("store: claim invite: %w", err)
	}
	if n, err := res.RowsAffected(); err != nil {
		return fmt.Errorf("store: claim invite rows: %w", err)
	} else if n == 0 {
		return notFound("redeem_invite", "邀请")
	}
	// 角色只升不降：并发下不用读到的旧角色覆盖已升过的成员关系。
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO project_members (account_id, tenant_id, role, created_at)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(account_id, tenant_id) DO UPDATE SET
			role = CASE
				WHEN (CASE excluded.role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 ELSE 0 END)
				   > (CASE project_members.role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 ELSE 0 END)
				THEN excluded.role ELSE project_members.role END`,
		in.AccountID, in.TenantID, string(in.Role), encodeTime(in.Now)); err != nil {
		return fmt.Errorf("store: redeem member: %w", err)
	}
	if in.SessionID != "" {
		if _, err := tx.ExecContext(ctx,
			`UPDATE sessions SET current_tenant_id = ? WHERE id = ?`, in.TenantID, in.SessionID); err != nil {
			return fmt.Errorf("store: redeem session tenant: %w", err)
		}
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE accounts SET current_tenant_id = ? WHERE id = ?`, in.TenantID, in.AccountID); err != nil {
		return fmt.Errorf("store: redeem account tenant: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit invite: %w", err)
	}
	return nil
}

// PurgeInvitesBefore 删除过期早于 cutoff 的邀请；活跃、已用但未过期的记录都保留。
func (s *Store) PurgeInvitesBefore(ctx context.Context, before time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx, `DELETE FROM project_invites WHERE expires_at < ?`, encodeTime(before))
	if err != nil {
		return 0, fmt.Errorf("store: purge invites: %w", err)
	}
	return res.RowsAffected()
}

// SearchAccounts 按“用户名前缀 + 显示名子串”搜索账号，最多 limit 条。
// 空查询返回空切片，不做全表扫描；通配符按字面处理。
func (s *Store) SearchAccounts(ctx context.Context, query string, limit int) ([]domain.Account, error) {
	query = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(query), "@"))
	if query == "" || limit <= 0 {
		return nil, nil
	}
	escaper := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	term := strings.ToLower(escaper.Replace(query))
	rows, err := s.read.QueryContext(ctx, `
		SELECT `+accountColumns+` FROM accounts
		WHERE lower(username) LIKE ? ESCAPE '\' OR lower(display_name) LIKE ? ESCAPE '\'
		ORDER BY username LIMIT ?`,
		term+"%", "%"+term+"%", limit)
	if err != nil {
		return nil, fmt.Errorf("store: search accounts: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []domain.Account
	for rows.Next() {
		acct, err := scanAccount(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan account: %w", err)
		}
		out = append(out, acct)
	}
	return out, rows.Err()
}

func scanInvite(row scanner) (domain.ProjectInvite, error) {
	var (
		inv               domain.ProjectInvite
		role              string
		hash              []byte
		created, expires  string
		usedAt, revokedAt string
	)
	if err := row.Scan(&inv.ID, &inv.TenantID, &role, &hash, &inv.CreatedByAccountID, &created,
		&expires, &usedAt, &inv.UsedByAccountID, &revokedAt); err != nil {
		return domain.ProjectInvite{}, err
	}
	if len(hash) != len(inv.TokenHash) {
		return domain.ProjectInvite{}, fmt.Errorf("store: invite hash length %d", len(hash))
	}
	copy(inv.TokenHash[:], hash)
	inv.Role = domain.Role(role)
	var err error
	if inv.CreatedAt, err = decodeTime(created); err != nil {
		return domain.ProjectInvite{}, err
	}
	if inv.ExpiresAt, err = decodeTime(expires); err != nil {
		return domain.ProjectInvite{}, err
	}
	if err := setOptionalTime(&inv.UsedAt, usedAt); err != nil {
		return domain.ProjectInvite{}, err
	}
	if err := setOptionalTime(&inv.RevokedAt, revokedAt); err != nil {
		return domain.ProjectInvite{}, err
	}
	return inv, nil
}
