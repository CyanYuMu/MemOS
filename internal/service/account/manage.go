package account

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"

	"github.com/alexedwards/argon2id"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// CreateAccountInput 是创建账号的入参。
type CreateAccountInput struct {
	Username      string
	Password      string
	DisplayName   string
	PlatformAdmin bool
}

// UpdateAccountInput 里为 nil 的字段保持不变。
type UpdateAccountInput struct {
	DisplayName   *string
	Disabled      *bool
	PlatformAdmin *bool
}

// CreateAccount 校验用户名与口令后创建账号，用户名重复返回 ErrConflict。
func (s *Service) CreateAccount(ctx context.Context, in CreateAccountInput) (domain.Account, error) {
	name, err := normalizeUsername(in.Username)
	if err != nil {
		return domain.Account{}, err
	}
	if err := validatePassword(in.Password); err != nil {
		return domain.Account{}, err
	}
	hash, err := argon2id.CreateHash(in.Password, s.opts.Argon2)
	if err != nil {
		return domain.Account{}, err
	}
	display := strings.TrimSpace(in.DisplayName)
	if display == "" {
		display = name
	}
	acct := domain.Account{
		ID:            domain.NewID(),
		Username:      name,
		DisplayName:   display,
		PasswordHash:  hash,
		PlatformAdmin: in.PlatformAdmin,
		CreatedAt:     s.now(),
	}
	if err := s.store.CreateAccount(ctx, acct); err != nil {
		return domain.Account{}, err
	}
	if err := s.ensurePersonalTenant(ctx, acct); err != nil {
		// 账号已经建好，不能因为个人项目失败就让调用方以为建号失败；启动与登录会重试。
		s.log.WarnContext(ctx, "ensure personal project after create failed",
			slog.String("account_id", acct.ID), slog.Any("err", err))
	}
	return acct, nil
}

// UpdateAccount 修改显示名、禁用状态与平台管理员标记。
// 禁用账号会吊销其全部会话；最后一个启用的平台管理员不能被禁用或降级。
// actorAccountID 非空时不能自我降级或自我禁用，避免管理员把自己锁在门外。
func (s *Service) UpdateAccount(
	ctx context.Context,
	actorAccountID,
	id string,
	in UpdateAccountInput,
) (domain.Account, error) {
	defer s.invalidatePrincipalCache()
	s.adminMu.Lock()
	defer s.adminMu.Unlock()

	acct, err := s.store.GetAccount(ctx, id)
	if err != nil {
		return domain.Account{}, err
	}
	if err := guardSelfAccountChange(actorAccountID, id, in); err != nil {
		return domain.Account{}, err
	}
	next := acct
	if in.DisplayName != nil {
		display := strings.TrimSpace(*in.DisplayName)
		if display == "" {
			return domain.Account{}, domain.Invalid("update_account", "显示名不能为空")
		}
		next.DisplayName = display
	}
	if in.Disabled != nil {
		next.Disabled = *in.Disabled
	}
	if in.PlatformAdmin != nil {
		next.PlatformAdmin = *in.PlatformAdmin
	}
	if err := s.guardLastPlatformAdmin(ctx, acct, next); err != nil {
		return domain.Account{}, err
	}
	if next.Disabled && !acct.Disabled {
		// 禁用与吊销会话在同一事务里完成：不会出现账号已禁用但旧会话仍有效的中间态。
		if err := s.store.UpdateAccountAndRevokeSessions(ctx, next, s.now()); err != nil {
			return domain.Account{}, err
		}
		s.throttle.Clear(ctx, acct.Username)
	} else if err := s.store.UpdateAccount(ctx, next); err != nil {
		return domain.Account{}, err
	}
	if !next.Disabled && acct.Disabled {
		// 重新启用后补回个人项目；被禁用期间可能还没补建过。
		if err := s.ensurePersonalTenant(ctx, next); err != nil {
			s.log.WarnContext(ctx, "ensure personal project after enable failed",
				slog.String("account_id", id), slog.Any("err", err))
		}
	}
	return next, nil
}

func guardSelfAccountChange(actorAccountID, id string, in UpdateAccountInput) error {
	if actorAccountID == "" || actorAccountID != id {
		return nil
	}
	if in.PlatformAdmin != nil && !*in.PlatformAdmin {
		return domain.Wrap(domain.ErrConflict, "update_account", "不能取消自己的平台管理员权限", nil)
	}
	if in.Disabled != nil && *in.Disabled {
		return domain.Wrap(domain.ErrConflict, "update_account", "不能禁用自己的账号", nil)
	}
	return nil
}

func (s *Service) guardLastPlatformAdmin(ctx context.Context, before, after domain.Account) error {
	wasAdmin := before.PlatformAdmin && !before.Disabled
	stillAdmin := after.PlatformAdmin && !after.Disabled
	if !wasAdmin || stillAdmin {
		return nil
	}
	n, err := s.store.CountEnabledPlatformAdmins(ctx)
	if err != nil {
		return err
	}
	if n <= 1 {
		return domain.Wrap(domain.ErrConflict, "update_account", "至少要保留一个启用的平台管理员", nil)
	}
	return nil
}

// DeleteAccount 删除账号。两道业务约束：不能删自己（避免管理员把自己锁在门外），
// 不能删最后一个启用的平台管理员。个人项目随账号一起删除，团队项目里只移除成员关系，
// 账号创建过的 API Key 与邀请保留（团队可能还在用），仅清空创建者引用。
func (s *Service) DeleteAccount(ctx context.Context, actorAccountID, id string) error {
	defer s.invalidatePrincipalCache()
	s.adminMu.Lock()
	defer s.adminMu.Unlock()

	if actorAccountID != "" && actorAccountID == id {
		return domain.Wrap(domain.ErrConflict, "delete_account", "不能删除自己的账号", nil)
	}
	acct, err := s.store.GetAccount(ctx, id)
	if err != nil {
		return err
	}
	if acct.PlatformAdmin && !acct.Disabled {
		n, err := s.store.CountEnabledPlatformAdmins(ctx)
		if err != nil {
			return err
		}
		if n <= 1 {
			return domain.Wrap(domain.ErrConflict, "delete_account", "至少要保留一个启用的平台管理员", nil)
		}
	}
	if err := s.store.DeleteAccount(ctx, id); err != nil {
		return err
	}
	s.throttle.Clear(ctx, acct.Username)
	return nil
}

// ResetPassword 重置口令并吊销该账号的全部会话。
func (s *Service) ResetPassword(ctx context.Context, id, password string) error {
	defer s.invalidatePrincipalCache()
	if err := validatePassword(password); err != nil {
		return err
	}
	// 哈希在锁外算，管理员重置不同账号时不再互相排队。
	hash, err := argon2id.CreateHash(password, s.opts.Argon2)
	if err != nil {
		return err
	}
	s.adminMu.Lock()
	defer s.adminMu.Unlock()
	acct, err := s.store.GetAccount(ctx, id)
	if err != nil {
		return err
	}
	acct.PasswordHash = hash
	// 改口令与吊销旧会话同一事务：任一步失败都回滚，旧会话不会在新口令生效后继续有效。
	if err := s.store.UpdateAccountAndRevokeSessions(ctx, acct, s.now()); err != nil {
		return err
	}
	// 重置口令是管理员解围手段：清空失败计数，否则被锁定的账号（或出口 IP）仍然登不进来。
	s.throttle.Reset(ctx)
	return nil
}

// ListAccounts 列出全部账号。
func (s *Service) ListAccounts(ctx context.Context) ([]domain.Account, error) {
	return s.store.ListAccounts(ctx)
}

// ResolveAccount 按账号 ID 或用户名查找账号。
func (s *Service) ResolveAccount(ctx context.Context, idOrUsername string) (domain.Account, error) {
	acct, err := s.store.GetAccount(ctx, idOrUsername)
	if err == nil || !errors.Is(err, domain.ErrNotFound) {
		return acct, err
	}
	name, nameErr := normalizeUsername(idOrUsername)
	if nameErr != nil {
		return domain.Account{}, domain.NotFound("resolve_account", "账号不存在")
	}
	return s.store.AccountByUsername(ctx, name)
}

// MemberInput 是成员变更入参；ActorAccountID 为空表示不做自降检查。
type MemberInput struct {
	ActorAccountID string
	TenantID       string
	AccountID      string
	Role           domain.Role
}

// SetMember 把账号加入项目空间或修改其角色，重复调用是改角色。
// 把最后一个 owner 降级会被拒绝，避免项目只剩平台管理员才能恢复；
// ActorAccountID 非空时也不能把自己降级，即使项目还有其他 owner。
func (s *Service) SetMember(ctx context.Context, in MemberInput) error {
	defer s.invalidatePrincipalCache()
	lock := s.tenantMemberLock(in.TenantID)
	lock.Lock()
	defer lock.Unlock()
	if !in.Role.Valid() {
		return domain.Invalid("set_member", "角色必须是 viewer、editor 或 owner")
	}
	if in.ActorAccountID != "" && in.ActorAccountID == in.AccountID && in.Role != domain.RoleOwner {
		return domain.Wrap(domain.ErrConflict, "set_member", "不能降低自己的项目角色", nil)
	}
	if _, err := s.store.GetAccount(ctx, in.AccountID); err != nil {
		return err
	}
	if _, err := s.store.GetTenant(ctx, in.TenantID); err != nil {
		return err
	}
	if in.Role != domain.RoleOwner {
		if err := s.guardLastOwner(ctx, in.TenantID, in.AccountID); err != nil {
			return err
		}
	}
	return s.store.PutMember(ctx, domain.Membership{
		AccountID: in.AccountID, TenantID: in.TenantID, Role: in.Role, CreatedAt: s.now(),
	})
}

// RemoveMember 移出成员，关系不存在返回 ErrNotFound；最后一个 owner 不能被移除，
// actorAccountID 非空时也不能把自己移出项目。
func (s *Service) RemoveMember(ctx context.Context, actorAccountID, tenantID, accountID string) error {
	defer s.invalidatePrincipalCache()
	lock := s.tenantMemberLock(tenantID)
	lock.Lock()
	defer lock.Unlock()
	if actorAccountID != "" && actorAccountID == accountID {
		return domain.Wrap(domain.ErrConflict, "remove_member", "不能把自己移出项目", nil)
	}
	if err := s.guardLastOwner(ctx, tenantID, accountID); err != nil {
		return err
	}
	return s.store.RemoveMember(ctx, accountID, tenantID)
}

// LeaveProject 让账号主动退出项目。最后一个 owner 仍受 guardLastOwner 保护：
// 要退出必须先转让 owner，不能把项目变成无人管理。
func (s *Service) LeaveProject(ctx context.Context, accountID, tenantID string) error {
	if strings.TrimSpace(accountID) == "" {
		return domain.Invalid("leave_project", "缺少账号")
	}
	if strings.TrimSpace(tenantID) == "" {
		return domain.Invalid("leave_project", "缺少项目")
	}
	return s.RemoveMember(ctx, "", tenantID, accountID)
}

// tenantMemberLock 返回项目自己的成员写锁，保证同项目的“统计 owner + 写”是原子的，
// 不同项目之间不互斥。
func (s *Service) tenantMemberLock(tenantID string) *sync.Mutex {
	s.memberMu.Lock()
	defer s.memberMu.Unlock()
	if s.memberLocks == nil {
		s.memberLocks = make(map[string]*sync.Mutex)
	}
	lock := s.memberLocks[tenantID]
	if lock == nil {
		lock = &sync.Mutex{}
		s.memberLocks[tenantID] = lock
	}
	return lock
}

// guardLastOwner 在 accountID 是该项目唯一 owner 时返回冲突。
func (s *Service) guardLastOwner(ctx context.Context, tenantID, accountID string) error {
	members, err := s.store.ListMembers(ctx, tenantID)
	if err != nil {
		return err
	}
	owners, target := 0, false
	for _, m := range members {
		if m.Role == domain.RoleOwner {
			owners++
			if m.AccountID == accountID {
				target = true
			}
		}
	}
	if target && owners <= 1 {
		return domain.Wrap(domain.ErrConflict, "membership", "项目至少要保留一个 owner", nil)
	}
	return nil
}

// ListMembers 列出项目空间的成员关系。
func (s *Service) ListMembers(ctx context.Context, tenantID string) ([]domain.Membership, error) {
	return s.store.ListMembers(ctx, tenantID)
}

// MemberRoles 返回账号在全部项目里的角色，包含已停用与已归档项目。
// Principal.Roles 会过滤掉停用/归档项目；归档项目的列表与恢复入口需要完整成员关系，
// 否则 owner 在删除（归档）后连自己删掉的项目的入口都看不到。
func (s *Service) MemberRoles(ctx context.Context, accountID string) (map[string]domain.Role, error) {
	members, err := s.store.MembershipsOf(ctx, accountID)
	if err != nil {
		return nil, err
	}
	out := make(map[string]domain.Role, len(members))
	for _, m := range members {
		if m.Role.Valid() {
			out[m.TenantID] = m.Role
		}
	}
	return out, nil
}

// RevokeSessions 吊销账号的全部会话。
func (s *Service) RevokeSessions(ctx context.Context, accountID string) error {
	defer s.invalidatePrincipalCache()
	if _, err := s.store.GetAccount(ctx, accountID); err != nil {
		return err
	}
	return s.store.RevokeAccountSessions(ctx, accountID, s.now())
}
