package store

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const accountColumns = `id, username, display_name, password_hash, platform_admin, disabled, created_at, last_login_at, current_tenant_id, email`

// CreateAccount 写入账号。用户名唯一（存小写），重复返回 ErrConflict。
func (s *Store) CreateAccount(ctx context.Context, a domain.Account) error {
	created := a.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO accounts (`+accountColumns+`)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		a.ID, a.Username, a.DisplayName, a.PasswordHash, a.PlatformAdmin, a.Disabled,
		encodeTime(created), optionalTimeText(a.LastLoginAt), a.CurrentTenantID, a.Email)
	if err != nil {
		if isUniqueViolation(err) {
			return conflict("create_account", "用户名已存在", err)
		}
		return fmt.Errorf("store: create account: %w", err)
	}
	return nil
}

// GetAccount 按 ID 读取账号。
func (s *Store) GetAccount(ctx context.Context, id string) (domain.Account, error) {
	a, err := scanAccount(s.read.QueryRowContext(ctx,
		`SELECT `+accountColumns+` FROM accounts WHERE id = ?`, id))
	if err != nil {
		if isNotFound(err) {
			return domain.Account{}, notFound("get_account", "账号")
		}
		return domain.Account{}, fmt.Errorf("store: get account: %w", err)
	}
	return a, nil
}

// AccountByUsername 按用户名读取账号，调用方负责先转成小写。
func (s *Store) AccountByUsername(ctx context.Context, username string) (domain.Account, error) {
	a, err := scanAccount(s.read.QueryRowContext(ctx,
		`SELECT `+accountColumns+` FROM accounts WHERE username = ?`, username))
	if err != nil {
		if isNotFound(err) {
			return domain.Account{}, notFound("account_by_username", "账号")
		}
		return domain.Account{}, fmt.Errorf("store: account by username: %w", err)
	}
	return a, nil
}

// ListAccounts 按创建时间升序列出全部账号。
func (s *Store) ListAccounts(ctx context.Context) ([]domain.Account, error) {
	rows, err := s.read.QueryContext(ctx,
		`SELECT `+accountColumns+` FROM accounts ORDER BY created_at ASC, id ASC`)
	if err != nil {
		return nil, fmt.Errorf("store: list accounts: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []domain.Account
	for rows.Next() {
		a, err := scanAccount(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan account: %w", err)
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// ListAccountsPage 在 SQL 里按用户名/显示名过滤并分页，返回当页数据与过滤后的总数。
//
// 旧实现在 Go 里对 ListAccounts 的全量结果做 Contains 与切片：账号一多，每次翻页
// 都要把整张表读进内存。这里把过滤与分页下推给 SQLite，排序保持 created_at ASC,
// id ASC；LIMIT/OFFSET 由调用方按页码换算。COUNT 与分页在同一个只读事务里执行，
// 避免两次查询之间插入/停用账号导致 total 与当页条数对不上。
//
// LIKE 的大小写不敏感只覆盖 ASCII：SQLite 不会折叠非 ASCII 拉丁字母（如 Ä/ä），
// 中文等无大小写的字符不受影响。查询串里的 % 与 _ 按字面处理，不当通配符。
func (s *Store) ListAccountsPage(ctx context.Context, query string, limit, offset int) ([]domain.Account, int, error) {
	where := ""
	var args []any
	if q := strings.TrimSpace(query); q != "" {
		escaper := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
		pattern := "%" + escaper.Replace(q) + "%"
		where = ` WHERE (username LIKE ? ESCAPE '\' OR display_name LIKE ? ESCAPE '\')`
		args = []any{pattern, pattern}
	}
	offset = max(offset, 0)

	tx, err := s.read.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, 0, fmt.Errorf("store: begin accounts page: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var total int
	if err := tx.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM accounts`+where, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("store: count accounts: %w", err)
	}

	pageArgs := append(append([]any(nil), args...), limit, offset)
	rows, err := tx.QueryContext(ctx,
		`SELECT `+accountColumns+` FROM accounts`+where+
			` ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?`, pageArgs...)
	if err != nil {
		return nil, 0, fmt.Errorf("store: list accounts page: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]domain.Account, 0, max(limit, 0))
	for rows.Next() {
		a, err := scanAccount(rows)
		if err != nil {
			return nil, 0, fmt.Errorf("store: scan account: %w", err)
		}
		out = append(out, a)
	}
	rowErr := rows.Err()
	// 先关 rows 再提交：SQLite 在语句未结束时提交会报 "SQL statements in progress"。
	// defer 里再关一次是无害的。
	if err := rows.Close(); err != nil {
		return nil, 0, fmt.Errorf("store: close accounts page: %w", err)
	}
	if rowErr != nil {
		return nil, 0, fmt.Errorf("store: list accounts page rows: %w", rowErr)
	}
	if err := tx.Commit(); err != nil {
		return nil, 0, fmt.Errorf("store: commit accounts page: %w", err)
	}
	return out, total, nil
}

// UpdateAccount 更新 display_name、password_hash、platform_admin、disabled。
func (s *Store) UpdateAccount(ctx context.Context, a domain.Account) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE accounts SET display_name = ?, password_hash = ?, platform_admin = ?, disabled = ?
		WHERE id = ?`,
		a.DisplayName, a.PasswordHash, a.PlatformAdmin, a.Disabled, a.ID)
	if err != nil {
		return fmt.Errorf("store: update account: %w", err)
	}
	return requireAffected(res, "update_account", "账号")
}

// UpdateAccountAndRevokeSessions 在同一个事务里更新账号并吊销其全部未吊销会话。
// 用于“由启用转禁用”与管理员重置口令：任一步失败都回滚，避免账号已禁用
// 或口令已更换、旧会话却仍然有效的中间态。
func (s *Store) UpdateAccountAndRevokeSessions(ctx context.Context, a domain.Account, at time.Time) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: update account and revoke sessions: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx, `
		UPDATE accounts SET display_name = ?, password_hash = ?, platform_admin = ?, disabled = ?
		WHERE id = ?`,
		a.DisplayName, a.PasswordHash, a.PlatformAdmin, a.Disabled, a.ID)
	if err != nil {
		return fmt.Errorf("store: update account: %w", err)
	}
	if err := requireAffected(res, "update_account", "账号"); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at = ''`,
		encodeTime(at), a.ID); err != nil {
		return fmt.Errorf("store: revoke account sessions: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit account revocation: %w", err)
	}
	return nil
}

// SetAccountProfile 更新外部身份带来的资料字段（显示名、邮箱），不碰口令与权限。
func (s *Store) SetAccountProfile(ctx context.Context, id, displayName, email string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE accounts SET display_name = ?, email = ? WHERE id = ?`, displayName, email, id)
	if err != nil {
		return fmt.Errorf("store: set account profile: %w", err)
	}
	return requireAffected(res, "set_account_profile", "账号")
}

// SetAccountTenant 修改账号的当前项目空间（外部身份没有会话时使用）。
func (s *Store) SetAccountTenant(ctx context.Context, id, tenantID string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE accounts SET current_tenant_id = ? WHERE id = ?`, tenantID, id)
	if err != nil {
		return fmt.Errorf("store: set account tenant: %w", err)
	}
	return requireAffected(res, "set_account_tenant", "账号")
}

// TouchLogin 记录最近一次登录时间。
func (s *Store) TouchLogin(ctx context.Context, id string, at time.Time) error {
	if _, err := s.db.ExecContext(ctx,
		`UPDATE accounts SET last_login_at = ? WHERE id = ?`, encodeTime(at), id); err != nil {
		return fmt.Errorf("store: touch login: %w", err)
	}
	return nil
}

// DeleteAccount 删除账号及其控制面附属数据：成员关系、会话、个人项目（连同该项目的
// 成员、邀请、API Key 与待执行任务）。账号创建的项目 Key/邀请保留，但把创建者引用清空，
// 避免删人把团队正在用的 Key 一起带走；审计记录保留。
//
// 调用方负责业务约束（不能删自己、不能删最后一个平台管理员），这里只保证数据一致。
func (s *Store) DeleteAccount(ctx context.Context, id string) error {
	if id == "" {
		return domain.Invalid("delete_account", "账号 id 不能为空")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: delete account: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var personal string
	err = tx.QueryRowContext(ctx,
		`SELECT id FROM tenants WHERE kind = 'personal' AND created_by_account_id = ?`, id).Scan(&personal)
	if err != nil && !isNotFound(err) {
		return fmt.Errorf("store: find personal tenant: %w", err)
	}
	if personal != "" {
		for _, q := range []string{
			`DELETE FROM project_invites WHERE tenant_id = ?`,
			`DELETE FROM project_members WHERE tenant_id = ?`,
			`DELETE FROM tokens WHERE tenant_id = ?`,
			`DELETE FROM jobs WHERE tenant_id = ? AND status IN ('pending', 'running')`,
			`UPDATE sessions SET current_tenant_id = '' WHERE current_tenant_id = ?`,
			`UPDATE accounts SET current_tenant_id = '' WHERE current_tenant_id = ?`,
			`DELETE FROM tenants WHERE id = ?`,
		} {
			if _, err := tx.ExecContext(ctx, q, personal); err != nil {
				return fmt.Errorf("store: delete personal tenant: %w", err)
			}
		}
	}
	for _, q := range []string{
		`DELETE FROM project_members WHERE account_id = ?`,
		`DELETE FROM sessions WHERE account_id = ?`,
		`UPDATE tokens SET created_by_account_id = '' WHERE created_by_account_id = ?`,
		`UPDATE project_invites SET created_by_account_id = '' WHERE created_by_account_id = ?`,
		`UPDATE project_invites SET used_by_account_id = '' WHERE used_by_account_id = ?`,
	} {
		if _, err := tx.ExecContext(ctx, q, id); err != nil {
			return fmt.Errorf("store: delete account references: %w", err)
		}
	}
	res, err := tx.ExecContext(ctx, `DELETE FROM accounts WHERE id = ?`, id)
	if err != nil {
		return fmt.Errorf("store: delete account: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: delete account: %w", err)
	}
	if affected == 0 {
		return notFound("delete_account", "账号")
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit delete account: %w", err)
	}
	return nil
}

// CountEnabledPlatformAdmins 返回启用的平台管理员账号数。
func (s *Store) CountEnabledPlatformAdmins(ctx context.Context) (int, error) {
	var n int
	if err := s.read.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM accounts WHERE platform_admin = 1 AND disabled = 0`).Scan(&n); err != nil {
		return 0, fmt.Errorf("store: count platform admins: %w", err)
	}
	return n, nil
}

// PutMember 幂等写入成员关系，已存在则更新角色。
func (s *Store) PutMember(ctx context.Context, m domain.Membership) error {
	created := m.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO project_members (account_id, tenant_id, role, created_at)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(account_id, tenant_id) DO UPDATE SET role = excluded.role`,
		m.AccountID, m.TenantID, string(m.Role), encodeTime(created))
	if err != nil {
		return fmt.Errorf("store: put member: %w", err)
	}
	return nil
}

// RemoveMember 删除成员关系，不存在返回 ErrNotFound。
func (s *Store) RemoveMember(ctx context.Context, accountID, tenantID string) error {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM project_members WHERE account_id = ? AND tenant_id = ?`, accountID, tenantID)
	if err != nil {
		return fmt.Errorf("store: remove member: %w", err)
	}
	return requireAffected(res, "remove_member", "成员关系")
}

// ListMembers 列出项目空间的成员关系。
func (s *Store) ListMembers(ctx context.Context, tenantID string) ([]domain.Membership, error) {
	rows, err := s.read.QueryContext(ctx, `
		SELECT account_id, tenant_id, role, created_at FROM project_members
		WHERE tenant_id = ? ORDER BY created_at ASC, account_id ASC`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("store: list members: %w", err)
	}
	return scanMembers(rows)
}

// MembershipsOf 列出账号加入的全部项目空间。
func (s *Store) MembershipsOf(ctx context.Context, accountID string) ([]domain.Membership, error) {
	rows, err := s.read.QueryContext(ctx, `
		SELECT account_id, tenant_id, role, created_at FROM project_members
		WHERE account_id = ? ORDER BY tenant_id ASC`, accountID)
	if err != nil {
		return nil, fmt.Errorf("store: memberships of account: %w", err)
	}
	return scanMembers(rows)
}

func scanMembers(rows *sql.Rows) ([]domain.Membership, error) {
	defer func() { _ = rows.Close() }()
	var out []domain.Membership
	for rows.Next() {
		var (
			m       domain.Membership
			role    string
			created string
		)
		if err := rows.Scan(&m.AccountID, &m.TenantID, &role, &created); err != nil {
			return nil, fmt.Errorf("store: scan member: %w", err)
		}
		m.Role = domain.Role(role)
		var err error
		if m.CreatedAt, err = decodeTime(created); err != nil {
			return nil, fmt.Errorf("store: member created_at: %w", err)
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

func scanAccount(row scanner) (domain.Account, error) {
	var (
		a                domain.Account
		created          string
		lastLogin        string
		platform, banned int
	)
	if err := row.Scan(&a.ID, &a.Username, &a.DisplayName, &a.PasswordHash,
		&platform, &banned, &created, &lastLogin, &a.CurrentTenantID, &a.Email); err != nil {
		return domain.Account{}, err
	}
	a.PlatformAdmin, a.Disabled = platform != 0, banned != 0
	var err error
	if a.CreatedAt, err = decodeTime(created); err != nil {
		return domain.Account{}, fmt.Errorf("account created_at: %w", err)
	}
	if err = setOptionalTime(&a.LastLoginAt, lastLogin); err != nil {
		return domain.Account{}, fmt.Errorf("account last_login_at: %w", err)
	}
	return a, nil
}

func requireAffected(res sql.Result, op, resource string) error {
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: %s rows: %w", op, err)
	}
	if n == 0 {
		return notFound(op, resource)
	}
	return nil
}
