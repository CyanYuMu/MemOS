package tenant

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const op = "tenant"

// Store 是项目注册表需要的存储端口，由 adapter/store 结构化实现。
type Store interface {
	GetTenant(ctx context.Context, id string) (domain.Tenant, error)
	ListTenants(ctx context.Context) ([]domain.Tenant, error)
	UpsertTenant(ctx context.Context, t domain.Tenant) error
	// CreateTenantWithOwner 在同一事务里插入项目与 owner 成员关系，失败整体回滚。
	CreateTenantWithOwner(ctx context.Context, t domain.Tenant, ownerAccountID string, role domain.Role) error
	// ArchiveTenant 软删除：写归档时间并停用访问，成员与 Key 保留。
	ArchiveTenant(ctx context.Context, id string, at time.Time) error
	// RestoreTenant 撤销归档并恢复启用。
	RestoreTenant(ctx context.Context, id string) error
}

// Accounts 把管理 Token 指定的 ownerUsername 解析成账号；由 account 服务实现。
type Accounts interface {
	ResolveAccount(ctx context.Context, idOrUsername string) (domain.Account, error)
}

// CreateInput 是创建团队项目的入参。
type CreateInput struct {
	Name        string
	Description string
	MultiView   bool
	// OwnerUsername 只在管理 Token 创建项目时使用：它没有账号身份，必须显式指定 owner。
	OwnerUsername string
}

// Patch 是项目设置的增量修改；nil 表示不动。
type Patch struct {
	Name        *string
	Description *string
	MultiView   *bool
	Disabled    *bool
}

// UpsertInput 是运维 upsert 的入参。Description 与 MultiView 为 nil 时保留原值。
type UpsertInput struct {
	ID          string
	UserID      string
	Name        string
	Disabled    bool
	Description *string
	MultiView   *bool
}

// Service 提供项目注册表的写用例。写入成功后调用 invalidate，让权限变化立即生效。
type Service struct {
	store      Store
	accounts   Accounts
	invalidate func()
	now        func() time.Time
}

// New 构造项目服务。invalidate 可为 nil；accounts 只在管理 Token 创建项目时用到，
// 但装配时应始终传入，避免该分支解引用 nil。
func New(store Store, accounts Accounts, invalidate func()) *Service {
	return &Service{store: store, accounts: accounts, invalidate: invalidate, now: time.Now}
}

// Get 读取单个项目。
func (s *Service) Get(ctx context.Context, id string) (domain.Tenant, error) {
	return s.store.GetTenant(ctx, id)
}

// List 返回全部项目；可见性过滤由调用方按主体决定。
func (s *Service) List(ctx context.Context) ([]domain.Tenant, error) {
	return s.store.ListTenants(ctx)
}

// Create 创建团队项目，创建者（或管理 Token 指定的 owner）在同一事务里成为 owner。
// 只写注册表，不调上游：memos-api 没有创建用户的接口，租户靠节点上的 user_name 隔离，
// 第一次写入就会产生对应的数据。
func (s *Service) Create(ctx context.Context, actor domain.Principal, in CreateInput) (domain.Tenant, error) {
	name := strings.TrimSpace(in.Name)
	switch {
	case name == "":
		return domain.Tenant{}, domain.Invalid(op, "项目名称不能为空")
	case !domain.ValidTenantID(name):
		return domain.Tenant{}, domain.Invalid(op, "项目名称只能包含字母、数字、点、下划线、连字符，长度 1..64")
	case domain.IsReservedTenantID(name):
		return domain.Tenant{}, domain.Invalid(op, domain.PersonalTenantPrefix+" 前缀保留给个人项目")
	}
	if _, err := s.store.GetTenant(ctx, name); err == nil {
		return domain.Tenant{}, domain.Invalid(op, "已存在同名项目")
	} else if !errors.Is(err, domain.ErrNotFound) {
		return domain.Tenant{}, err
	}
	ownerID, err := s.resolveOwner(ctx, actor, in.OwnerUsername)
	if err != nil {
		return domain.Tenant{}, err
	}
	t := domain.Tenant{
		ID:                 name,
		UserID:             name,
		Name:               name,
		Description:        in.Description,
		MultiView:          in.MultiView,
		Kind:               domain.TenantKindTeam,
		CreatedByAccountID: ownerID,
	}
	if err := s.store.CreateTenantWithOwner(ctx, t, ownerID, domain.RoleOwner); err != nil {
		if errors.Is(err, domain.ErrConflict) {
			// 预检查之后的并发同名创建：唯一约束兜底，保持与预检查一致的口径。
			return domain.Tenant{}, domain.Invalid(op, "已存在同名项目")
		}
		return domain.Tenant{}, err
	}
	s.changed()
	return s.store.GetTenant(ctx, name)
}

// resolveOwner 账号主体自己建项目并成为 owner；管理 Token 没有账号身份，必须显式给
// ownerUsername，否则会留下一个只有平台管理员能看到的无主项目。
func (s *Service) resolveOwner(ctx context.Context, actor domain.Principal, ref string) (string, error) {
	if actor.Kind != domain.PrincipalAdminToken {
		return actor.AccountID, nil
	}
	ref = strings.TrimSpace(ref)
	if ref == "" {
		return "", domain.Invalid(op, "管理 Token 创建项目需要 ownerUsername")
	}
	owner, err := s.accounts.ResolveAccount(ctx, ref)
	if err != nil {
		return "", err
	}
	if owner.Disabled {
		return "", domain.Invalid(op, "所有者账号已禁用")
	}
	return owner.ID, nil
}

// Update 修改项目设置。归档项目只能先恢复；个人项目不能停用。
func (s *Service) Update(ctx context.Context, id string, patch Patch) (domain.Tenant, error) {
	t, err := s.store.GetTenant(ctx, id)
	if err != nil {
		return domain.Tenant{}, err
	}
	if err := t.EnsureNotArchived(); err != nil {
		return domain.Tenant{}, err
	}
	if patch.Disabled != nil && *patch.Disabled {
		if err := t.EnsureDisablable(); err != nil {
			return domain.Tenant{}, err
		}
	}
	if patch.Name != nil {
		if !domain.ValidTenantID(*patch.Name) {
			return domain.Tenant{}, domain.Invalid(op, "项目名称格式非法")
		}
		t.Name = *patch.Name
	}
	if patch.Description != nil {
		t.Description = *patch.Description
	}
	if patch.MultiView != nil {
		t.MultiView = *patch.MultiView
	}
	if patch.Disabled != nil {
		t.Disabled = *patch.Disabled
	}
	if err := s.save(ctx, t); err != nil {
		return domain.Tenant{}, err
	}
	return t, nil
}

// Disable 回收（停用）项目。个人项目不能停用；对已归档项目是无操作。
func (s *Service) Disable(ctx context.Context, id string) error {
	t, err := s.store.GetTenant(ctx, id)
	if err != nil {
		return err
	}
	if err := t.EnsureDisablable(); err != nil {
		return err
	}
	t.Disabled = true
	return s.save(ctx, t)
}

// Archive 归档团队项目（软删除）。成员、邀请与 API Key 全部保留，之后可以经 Restore
// 原样恢复；上游没有删除 cube 的接口，记忆数据本来就留在数据面，归档不会碰它。
func (s *Service) Archive(ctx context.Context, id string) error {
	t, err := s.store.GetTenant(ctx, id)
	if err != nil {
		return err
	}
	if err := t.EnsureArchivable(); err != nil {
		return err
	}
	if err := s.store.ArchiveTenant(ctx, id, s.now()); err != nil {
		return err
	}
	s.changed()
	return nil
}

// Restore 撤销归档，项目回到启用状态。
func (s *Service) Restore(ctx context.Context, id string) error {
	t, err := s.store.GetTenant(ctx, id)
	if err != nil {
		return err
	}
	if !t.Archived() {
		return domain.Wrap(domain.ErrConflict, op, "项目未归档，无需恢复", nil)
	}
	if err := s.store.RestoreTenant(ctx, id); err != nil {
		return err
	}
	s.changed()
	return nil
}

// Upsert 是运维入口：创建或整体更新。name、user_id 与 disabled 以入参为准；未提供的
// description / multiView 以及已有项目的归档状态保留原值，不会被一次 upsert 悄悄清掉。
// 保留前缀只约束新建：平台管理员停用个人项目走的正是对已有个人项目的 upsert。
func (s *Service) Upsert(ctx context.Context, in UpsertInput) (domain.Tenant, error) {
	if !domain.ValidTenantID(in.ID) {
		return domain.Tenant{}, domain.Invalid(op, "id 只能包含字母、数字、点、下划线、连字符，长度 1..64")
	}
	if in.UserID == "" {
		return domain.Tenant{}, domain.Invalid(op, "user_id 不能为空")
	}
	name := in.Name
	if name == "" {
		name = in.UserID
	}
	t := domain.Tenant{ID: in.ID, UserID: in.UserID, Name: name, Disabled: in.Disabled}
	existing, err := s.store.GetTenant(ctx, in.ID)
	switch {
	case err == nil:
		t.Description, t.MultiView, t.ArchivedAt = existing.Description, existing.MultiView, existing.ArchivedAt
	case errors.Is(err, domain.ErrNotFound):
		if domain.IsReservedTenantID(in.ID) {
			return domain.Tenant{}, domain.Invalid(op, domain.PersonalTenantPrefix+" 前缀保留给个人项目")
		}
	default:
		return domain.Tenant{}, err
	}
	if in.Description != nil {
		t.Description = *in.Description
	}
	if in.MultiView != nil {
		t.MultiView = *in.MultiView
	}
	// user_id 直接映射上游 user_name，唯一性由 store 的部分唯一索引兜底，冲突原样透传。
	if err := s.save(ctx, t); err != nil {
		return domain.Tenant{}, err
	}
	return s.store.GetTenant(ctx, in.ID)
}

func (s *Service) save(ctx context.Context, t domain.Tenant) error {
	if err := s.store.UpsertTenant(ctx, t); err != nil {
		return err
	}
	s.changed()
	return nil
}

func (s *Service) changed() {
	if s.invalidate != nil {
		s.invalidate()
	}
}
