package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// upstreamUserIDTaken 是 tenants.user_id 唯一冲突的用户可见消息。
// user_id 直接映射上游 user_name；两个项目指到同一个上游用户就会互相串记忆数据，
// 因此唯一索引的冲突必须报成可辨认的 409，而不是笼统的“同名项目”或 500。
const upstreamUserIDTaken = "该上游 user_id 已被其他项目占用"

// personalOwnerTaken 是 tenants.created_by_account_id 部分唯一索引冲突的消息：
// 一个账号只能有一个个人项目。不区分的话会被当成“同名项目”，前端提示改名重试，
// 但改任何名字都仍然失败。
const personalOwnerTaken = "该账号已有个人项目"

// GetTenant 按租户 ID 读取。
func (s *Store) GetTenant(ctx context.Context, id string) (domain.Tenant, error) {
	row := s.read.QueryRowContext(ctx,
		`SELECT id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at
		 FROM tenants WHERE id = ?`, id)
	t, err := scanTenant(row)
	if err != nil {
		if isNotFound(err) {
			return domain.Tenant{}, notFound("get_tenant", "租户")
		}
		return domain.Tenant{}, fmt.Errorf("store: get tenant: %w", err)
	}
	return t, nil
}

// normalizeArchived 让 archived_at 与 disabled 保持一致：归档的项目一定不可用。
//
// 这两个字段是一个三态（可用 / 停用 / 归档）被拆成的两列，库上没有约束能保证
// “archived_at 非空 ⇒ disabled=1”。ArchiveTenant/RestoreTenant 成对写，这里再兜住
// 其它 upsert 入口，避免出现“已归档却能访问”。
func normalizeArchived(t *domain.Tenant) {
	if t.Archived() {
		t.Disabled = true
	}
}

// CreateTenant 只插入新租户，同名已存在时返回冲突。创建项目的入口用它，
// 避免“先查后写”的竞态把别人刚建好的项目覆盖掉。
func (s *Store) CreateTenant(ctx context.Context, t domain.Tenant) error {
	if t.ID == "" || t.UserID == "" {
		return domain.Invalid("create_tenant", "租户 id 与 user_id 不能为空")
	}
	normalizeArchived(&t)
	created := t.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO tenants (id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		t.ID, t.UserID, t.Name, t.Description, t.MultiView, t.Disabled, encodeTime(created),
		kindOf(t), t.CreatedByAccountID, archivedText(t.ArchivedAt))
	if err != nil {
		if isUniqueViolation(err) {
			if uniqueOn(err, "tenants.user_id") {
				return conflict("create_tenant", upstreamUserIDTaken, err)
			}
			return conflict("create_tenant", "已存在同名项目", err)
		}
		return fmt.Errorf("store: create tenant: %w", err)
	}
	return nil
}

// CreateTenantWithOwner 在同一个事务里插入租户行与 owner 成员行。
// 创建项目的入口用它，任何一步失败都整体回滚，不会留下没有 owner 的项目；
// 同名已存在仍返回 ErrConflict，与 CreateTenant 的语义一致。
func (s *Store) CreateTenantWithOwner(
	ctx context.Context,
	t domain.Tenant,
	ownerAccountID string,
	role domain.Role,
) error {
	if t.ID == "" || t.UserID == "" {
		return domain.Invalid("create_tenant_with_owner", "租户 id 与 user_id 不能为空")
	}
	if ownerAccountID == "" {
		return domain.Invalid("create_tenant_with_owner", "owner 账号不能为空")
	}
	if !role.Valid() {
		return domain.Invalid("create_tenant_with_owner", "角色必须是 viewer、editor 或 owner")
	}
	normalizeArchived(&t)
	created := t.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: create tenant with owner: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO tenants (id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		t.ID, t.UserID, t.Name, t.Description, t.MultiView, t.Disabled, encodeTime(created),
		kindOf(t), t.CreatedByAccountID, archivedText(t.ArchivedAt)); err != nil {
		if isUniqueViolation(err) {
			switch {
			case uniqueOn(err, "tenants.user_id"):
				return domain.Wrap(domain.ErrConflict, "create_tenant_with_owner", upstreamUserIDTaken, err)
			case uniqueOn(err, "tenants.created_by_account_id"):
				return domain.Wrap(domain.ErrConflict, "create_tenant_with_owner", personalOwnerTaken, err)
			}
			// 其余唯一冲突来自主键：项目 ID 就是项目名。
			return domain.Wrap(domain.ErrConflict, "create_tenant_with_owner", "已存在同名项目", nil)
		}
		return fmt.Errorf("store: insert tenant: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO project_members (account_id, tenant_id, role, created_at)
		VALUES (?, ?, ?, ?)`,
		ownerAccountID, t.ID, string(role), encodeTime(created)); err != nil {
		if isUniqueViolation(err) {
			return domain.Wrap(domain.ErrConflict, "create_tenant_with_owner", "该账号已是项目成员", nil)
		}
		return fmt.Errorf("store: insert tenant owner: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit tenant with owner: %w", err)
	}
	return nil
}

// UpsertTenant 写入或更新租户；created_at 只在首次写入时生效。
// kind/created_by_account_id 只在插入时写入：已有行（尤其个人项目）的类型与归属
// 不会被管理入口的 upsert 改写。
func (s *Store) UpsertTenant(ctx context.Context, t domain.Tenant) error {
	return s.upsertTenant(ctx, t, "upsert_tenant", `
			user_id = excluded.user_id,
			name = excluded.name,
			description = excluded.description,
			multi_view = excluded.multi_view,
			disabled = excluded.disabled,
			archived_at = excluded.archived_at`)
}

// EnsureTenant 只保证租户存在并同步 user_id/name，不重置 disabled。
// 用于启动时写入静态租户；管理员禁用/吊销的状态必须跨重启保留。
// description/multi_view 属于控制台编辑的数据，只在新插入时生效，已经存在时不覆盖。
// name 只在仍是空串或等于旧 user_id（启动配置生成的占位名）时同步，
// 否则会覆盖迁移导入或管理员改过的项目名。
func (s *Store) EnsureTenant(ctx context.Context, t domain.Tenant) error {
	return s.upsertTenant(ctx, t, "ensure_tenant", `
			user_id = excluded.user_id,
			name = CASE WHEN name = '' OR name = user_id THEN excluded.name ELSE name END`)
}

// upsertTenant 是 UpsertTenant/EnsureTenant 的共同实现：两者只在冲突更新列上不同。
func (s *Store) upsertTenant(ctx context.Context, t domain.Tenant, op, update string) error {
	if t.ID == "" || t.UserID == "" {
		return domain.Invalid(op, "租户 id 与 user_id 不能为空")
	}
	normalizeArchived(&t)
	created := t.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: %s: %w", op, err)
	}
	defer func() { _ = tx.Rollback() }()
	//nolint:gosec // update 只来自本文件两个固定片段，不含请求输入
	query := `
		INSERT INTO tenants (id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET` + update
	if _, err := tx.ExecContext(ctx, query,
		t.ID, t.UserID, t.Name, t.Description, t.MultiView, t.Disabled, encodeTime(created),
		kindOf(t), t.CreatedByAccountID, archivedText(t.ArchivedAt)); err != nil {
		if isUniqueViolation(err) {
			if uniqueOn(err, "tenants.user_id") {
				return conflict(op, upstreamUserIDTaken, err)
			}
			return conflict(op, "已存在同名项目", err)
		}
		return fmt.Errorf("store: %s: %w", op, err)
	}
	// tokens.user_id 是历史遗留的冗余快照（鉴权以 tenants.user_id 为准）。
	// 改绑上游用户时同步旧行，避免管理端列表继续显示过期值。
	if err := syncTokenUserID(ctx, tx, t); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit %s: %w", op, err)
	}
	return nil
}

// syncTokenUserID 让 tokens.user_id 跟随租户的当前值。该列是历史冗余快照，
// 鉴权已经以 tenants.user_id 为准；这里只保证管理端读到的快照不陈旧。
func syncTokenUserID(ctx context.Context, tx *sql.Tx, t domain.Tenant) error {
	if _, err := tx.ExecContext(ctx,
		`UPDATE tokens SET user_id = ? WHERE tenant_id = ? AND user_id <> ?`,
		t.UserID, t.ID, t.UserID); err != nil {
		return fmt.Errorf("store: sync token user_id: %w", err)
	}
	return nil
}

// ListTenants 按 id 升序返回全部租户。
func (s *Store) ListTenants(ctx context.Context) ([]domain.Tenant, error) {
	rows, err := s.read.QueryContext(ctx,
		`SELECT id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at
		 FROM tenants ORDER BY id`)
	if err != nil {
		return nil, fmt.Errorf("store: list tenants: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []domain.Tenant
	for rows.Next() {
		t, err := scanTenant(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan tenant: %w", err)
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// PersonalTenantOf 按账号查个人项目；没有时返回 ErrNotFound。
func (s *Store) PersonalTenantOf(ctx context.Context, accountID string) (domain.Tenant, error) {
	row := s.read.QueryRowContext(ctx,
		`SELECT id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at
		 FROM tenants WHERE kind = 'personal' AND created_by_account_id = ?`, accountID)
	t, err := scanTenant(row)
	if err != nil {
		if isNotFound(err) {
			return domain.Tenant{}, notFound("personal_tenant", "个人项目")
		}
		return domain.Tenant{}, fmt.Errorf("store: personal tenant: %w", err)
	}
	return t, nil
}

// ArchiveTenant 软删除一个团队项目：写入 archived_at 并置 disabled=1，成员、邀请与
// API Key 都保留，恢复后原样可用。重复归档幂等，并保留第一次的归档时间。
func (s *Store) ArchiveTenant(ctx context.Context, id string, at time.Time) error {
	if id == "" {
		return domain.Invalid("archive_tenant", "租户 id 不能为空")
	}
	res, err := s.db.ExecContext(ctx, `
		UPDATE tenants SET archived_at = ?, disabled = 1
		WHERE id = ? AND archived_at = ''`, encodeTime(at), id)
	if err != nil {
		return fmt.Errorf("store: archive tenant: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: archive tenant rows: %w", err)
	}
	if affected > 0 {
		return nil
	}
	if _, err := s.GetTenant(ctx, id); err != nil {
		return err
	}
	return nil // 已归档：保持第一次的时间
}

// RestoreTenant 撤销归档：清空 archived_at 并恢复为启用。重复恢复幂等。
// 归档前的 disabled 状态不单独保存，恢复即启用；需要再停用时由 owner 重新操作。
func (s *Store) RestoreTenant(ctx context.Context, id string) error {
	if id == "" {
		return domain.Invalid("restore_tenant", "租户 id 不能为空")
	}
	res, err := s.db.ExecContext(ctx, `
		UPDATE tenants SET archived_at = '', disabled = 0
		WHERE id = ? AND archived_at <> ''`, id)
	if err != nil {
		return fmt.Errorf("store: restore tenant: %w", err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: restore tenant rows: %w", err)
	}
	if affected > 0 {
		return nil
	}
	if _, err := s.GetTenant(ctx, id); err != nil {
		return err
	}
	return nil // 未归档：已经是可用状态
}

// EnsurePersonalTenant 在一个事务里保证账号有个人项目与 owner 成员关系。// 已有就原样返回（保留改名、描述等控制台数据）；并发下靠部分唯一索引只留一行。
func (s *Store) EnsurePersonalTenant(ctx context.Context, t domain.Tenant, m domain.Membership) (domain.Tenant, error) {
	if t.ID == "" || t.UserID == "" || t.CreatedByAccountID == "" {
		return domain.Tenant{}, domain.Invalid("ensure_personal", "个人项目 id、user_id 与创建者不能为空")
	}
	created := t.CreatedAt
	if created.IsZero() {
		created = time.Now()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Tenant{}, fmt.Errorf("store: ensure personal tenant: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	// 冲突目标是部分唯一索引，SQLite 与 PG 都支持这种写法；DO NOTHING 让并发调用
	// 只有一条插入成功，其余继续走下面的重读。
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO tenants (id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at)
		VALUES (?, ?, ?, ?, 0, 0, ?, 'personal', ?, '')
		ON CONFLICT(created_by_account_id) WHERE kind = 'personal' DO NOTHING`,
		t.ID, t.UserID, t.Name, t.Description, encodeTime(created), t.CreatedByAccountID); err != nil {
		return domain.Tenant{}, fmt.Errorf("store: insert personal tenant: %w", err)
	}
	row := tx.QueryRowContext(ctx,
		`SELECT id, user_id, name, description, multi_view, disabled, created_at, kind, created_by_account_id, archived_at
		 FROM tenants WHERE kind = 'personal' AND created_by_account_id = ?`, t.CreatedByAccountID)
	stored, err := scanTenant(row)
	if err != nil {
		return domain.Tenant{}, fmt.Errorf("store: read personal tenant: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO project_members (account_id, tenant_id, role, created_at)
		VALUES (?, ?, 'owner', ?)
		ON CONFLICT(account_id, tenant_id) DO UPDATE SET role = 'owner'`,
		m.AccountID, stored.ID, encodeTime(created)); err != nil {
		return domain.Tenant{}, fmt.Errorf("store: personal tenant member: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return domain.Tenant{}, fmt.Errorf("store: commit personal tenant: %w", err)
	}
	return stored, nil
}

func kindOf(t domain.Tenant) domain.TenantKind {
	if t.Kind == "" {
		return domain.TenantKindTeam
	}
	return t.Kind
}

type scanner interface {
	Scan(dest ...any) error
}

func scanTenant(row scanner) (domain.Tenant, error) {
	var (
		t        domain.Tenant
		created  string
		kind     string
		archived string
	)
	if err := row.Scan(&t.ID, &t.UserID, &t.Name, &t.Description, &t.MultiView, &t.Disabled, &created,
		&kind, &t.CreatedByAccountID, &archived); err != nil {
		return domain.Tenant{}, err
	}
	parsed, err := decodeTime(created)
	if err != nil {
		return domain.Tenant{}, err
	}
	t.CreatedAt = parsed
	t.Kind = domain.TenantKind(kind)
	if t.Kind == "" {
		t.Kind = domain.TenantKindTeam
	}
	if archived != "" {
		at, err := decodeTime(archived)
		if err != nil {
			return domain.Tenant{}, err
		}
		t.ArchivedAt = &at
	}
	return t, nil
}

// archivedText 把可选归档时间写成文本：tenants.archived_at 是 NOT NULL DEFAULT ”，
// 空串表示未归档。
func archivedText(t *time.Time) string {
	if t == nil || t.IsZero() {
		return ""
	}
	return encodeTime(*t)
}
