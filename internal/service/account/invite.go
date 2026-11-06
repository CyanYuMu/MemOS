package account

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	// defaultInviteTTL 是创建邀请未指定过期时间时的有效期。
	defaultInviteTTL = 168 * time.Hour
	// defaultInviteMaxTTL 是邀请允许的最长有效期。
	defaultInviteMaxTTL = 720 * time.Hour
	// invitePurgeAfter 是过期邀请的保留时长：过期后再留 30 天便于排查。
	invitePurgeAfter = 30 * 24 * time.Hour
)

// CreateInviteInput 是创建邀请的入参；ExpiresAt 为零取默认有效期。
type CreateInviteInput struct {
	Actor     domain.Principal
	TenantID  string
	Role      domain.Role
	ExpiresAt time.Time
}

// CreatedInvite 是创建结果；Token/Path 只在这里出现一次，之后无法回读。
type CreatedInvite struct {
	Invite domain.ProjectInvite
	Token  string
	Path   string
}

// InvitePreview 是落地页需要的只读信息；AlreadyMember 表示当前主体已在项目里。
type InvitePreview struct {
	Invite        domain.ProjectInvite
	Tenant        domain.Tenant
	AlreadyMember bool
}

// CreateInvite 生成一条通用邀请链接。角色不高于 owner；owner 邀请只有平台管理员能建。
func (s *Service) CreateInvite(ctx context.Context, in CreateInviteInput) (CreatedInvite, error) {
	if err := in.Actor.RequireCap(in.TenantID, domain.CapProjectManage); err != nil {
		return CreatedInvite{}, err
	}
	tenant, err := s.store.GetTenant(ctx, in.TenantID)
	if err != nil {
		return CreatedInvite{}, err
	}
	if tenant.Archived() {
		return CreatedInvite{}, domain.Wrap(domain.ErrForbidden, "create_invite", "项目已归档，请先恢复", nil)
	}
	if tenant.Disabled {
		return CreatedInvite{}, domain.Wrap(domain.ErrForbidden, "create_invite", "项目已停用", nil)
	}
	if !in.Role.Valid() {
		return CreatedInvite{}, domain.Invalid("create_invite", "角色必须是 viewer、editor 或 owner")
	}
	if in.Role == domain.RoleOwner && !in.Actor.PlatformAdmin {
		return CreatedInvite{}, domain.Wrap(domain.ErrForbidden, "create_invite", "只有平台管理员可以创建 owner 邀请", nil)
	}
	now := s.now()
	expires := in.ExpiresAt
	if expires.IsZero() {
		expires = now.Add(s.opts.InviteDefaultTTL)
	}
	if !expires.After(now) {
		return CreatedInvite{}, domain.Invalid("create_invite", "过期时间必须晚于当前时间")
	}
	if expires.After(now.Add(s.opts.InviteMaxTTL)) {
		return CreatedInvite{}, domain.Invalid("create_invite", "过期时间不能超过允许的最长有效期")
	}
	token, err := domain.NewInviteToken()
	if err != nil {
		return CreatedInvite{}, err
	}
	inv := domain.ProjectInvite{
		ID:                 domain.NewID(),
		TenantID:           tenant.ID,
		Role:               in.Role,
		TokenHash:          domain.HashInviteToken(token),
		CreatedByAccountID: in.Actor.AccountID,
		CreatedAt:          now,
		ExpiresAt:          expires,
	}
	if err := s.store.CreateInvite(ctx, inv); err != nil {
		return CreatedInvite{}, err
	}
	return CreatedInvite{Invite: inv, Token: token, Path: "/__invite/" + token}, nil
}

// ListInvites 返回项目的全部邀请（含已用/已过期/已撤销，便于追溯）。
func (s *Service) ListInvites(
	ctx context.Context,
	actor domain.Principal,
	tenantID string,
) ([]domain.ProjectInvite, error) {
	if err := actor.RequireCap(tenantID, domain.CapProjectManage); err != nil {
		return nil, err
	}
	return s.store.ListInvites(ctx, tenantID)
}

// RevokeInvite 撤销一条未使用的邀请；已撤销幂等成功，已使用返回冲突。
func (s *Service) RevokeInvite(ctx context.Context, actor domain.Principal, tenantID, inviteID string) error {
	if err := actor.RequireCap(tenantID, domain.CapProjectManage); err != nil {
		return err
	}
	inv, err := s.store.GetInvite(ctx, inviteID)
	if err != nil {
		return err
	}
	if inv.TenantID != tenantID {
		return domain.NotFound("revoke_invite", "邀请不存在")
	}
	switch inv.Status(s.now()) {
	case domain.InviteRevoked:
		return nil
	case domain.InviteUsed:
		return domain.Wrap(domain.ErrConflict, "revoke_invite", "邀请已被使用，无法撤销", nil)
	case domain.InviteActive, domain.InviteExpired:
		// 过期的邀请已经不可兑换，撤销只是把状态落成 revoked，幂等且无副作用。
	}
	if err := s.store.RevokeInvite(ctx, tenantID, inviteID, s.now()); err != nil {
		return err
	}
	return nil
}

// InviteRoleInput 是修改邀请角色的入参。
type InviteRoleInput struct {
	Actor    domain.Principal
	TenantID string
	InviteID string
	Role     domain.Role
}

// UpdateInviteRole 修改一条尚未使用的邀请的角色。已使用的邀请不再改角色——
// 那会绕过“成员角色”接口对最后一个 owner 的保护，加入者应按成员改角色。
// owner 邀请仍然只有平台管理员可以发出，与 CreateInvite 同口径。
func (s *Service) UpdateInviteRole(ctx context.Context, in InviteRoleInput) error {
	if err := in.Actor.RequireCap(in.TenantID, domain.CapProjectManage); err != nil {
		return err
	}
	if !in.Role.Valid() {
		return domain.Invalid("update_invite_role", "角色必须是 viewer、editor 或 owner")
	}
	if in.Role == domain.RoleOwner && !in.Actor.PlatformAdmin {
		return domain.Wrap(domain.ErrForbidden, "update_invite_role", "只有平台管理员可以创建 owner 邀请", nil)
	}
	inv, err := s.store.GetInvite(ctx, in.InviteID)
	if err != nil {
		return err
	}
	if inv.TenantID != in.TenantID {
		return domain.NotFound("update_invite_role", "邀请不存在")
	}
	if inv.Status(s.now()) != domain.InviteActive {
		return domain.Wrap(domain.ErrConflict, "update_invite_role", "只有未使用的有效邀请可以改角色", nil)
	}
	return s.store.UpdateInviteRole(ctx, in.TenantID, in.InviteID, in.Role)
}

// PreviewInvite 只读地展示邀请信息，不消费。失效邀请统一返回 NotFound，
// 避免泄露“这条邀请曾经存在”；只有已使用且属于本人时返回 AlreadyMember。
func (s *Service) PreviewInvite(ctx context.Context, actor domain.Principal, rawToken string) (InvitePreview, error) {
	inv, err := s.lookupInvite(ctx, rawToken)
	if err != nil {
		return InvitePreview{}, err
	}
	status := inv.Status(s.now())
	if status != domain.InviteActive {
		if status != domain.InviteUsed || actor.AccountID == "" || actor.AccountID != inv.UsedByAccountID {
			return InvitePreview{}, inviteInvalid()
		}
		tenant, err := s.store.GetTenant(ctx, inv.TenantID)
		if err != nil || tenant.Disabled {
			return InvitePreview{}, inviteInvalid()
		}
		// 兑换后被移出项目的人不再算成员：不能凭历史令牌重新进入。
		if !s.isMember(ctx, actor.AccountID, inv.TenantID) {
			return InvitePreview{}, inviteInvalid()
		}
		return InvitePreview{Invite: inv, Tenant: tenant, AlreadyMember: true}, nil
	}
	tenant, err := s.store.GetTenant(ctx, inv.TenantID)
	if err != nil {
		return InvitePreview{}, inviteInvalid()
	}
	if tenant.Disabled {
		return InvitePreview{}, inviteInvalid()
	}
	already := actor.AccountID != "" && actor.RoleIn(inv.TenantID) != ""
	return InvitePreview{Invite: inv, Tenant: tenant, AlreadyMember: already}, nil
}

// RedeemInvite 兑换邀请：令牌只能成功消费一次，成员角色只升不降。
// 本人重复点击是幂等的；其他账号再用同一邀请一律失效。
func (s *Service) RedeemInvite(
	ctx context.Context,
	actor domain.Principal,
	rawToken string,
) (domain.ProjectInvite, error) {
	if actor.Kind != domain.PrincipalAccount || actor.AccountID == "" {
		return domain.ProjectInvite{}, domain.Wrap(domain.ErrForbidden, "redeem_invite", "管理 Token 不能兑换邀请", nil)
	}
	inv, err := s.lookupInvite(ctx, rawToken)
	if err != nil {
		return domain.ProjectInvite{}, err
	}
	tenant, err := s.store.GetTenant(ctx, inv.TenantID)
	if err != nil {
		return domain.ProjectInvite{}, inviteInvalid()
	}
	if tenant.Archived() {
		return domain.ProjectInvite{}, domain.Wrap(domain.ErrForbidden, "redeem_invite", "项目已归档，请先恢复", nil)
	}
	if tenant.Disabled {
		return domain.ProjectInvite{}, domain.Wrap(domain.ErrForbidden, "redeem_invite", "项目已停用", nil)
	}
	now := s.now()
	if inv.Status(now) == domain.InviteUsed && inv.UsedByAccountID == actor.AccountID {
		// 重放只切换当前项目，绝不重建成员关系：兑换后被移出项目的人拿着旧链接
		// 不能再把自己加回来（owner 邀请尤其危险）。成员关系已不存在时按失效处理。
		if !s.isMember(ctx, actor.AccountID, inv.TenantID) {
			return domain.ProjectInvite{}, inviteInvalid()
		}
		if err := s.switchTenant(ctx, actor, inv.TenantID); err != nil {
			return domain.ProjectInvite{}, err
		}
		return inv, nil
	}
	if inv.Status(now) != domain.InviteActive {
		return domain.ProjectInvite{}, inviteInvalid()
	}
	err = s.store.RedeemInvite(ctx, domain.RedeemInviteInput{
		InviteID:  inv.ID,
		TenantID:  inv.TenantID,
		Role:      inv.Role,
		AccountID: actor.AccountID,
		SessionID: actor.SessionID,
		Now:       now,
	})
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.ProjectInvite{}, inviteInvalid()
		}
		return domain.ProjectInvite{}, err
	}
	s.invalidatePrincipalCache()
	inv.UsedAt = &now
	inv.UsedByAccountID = actor.AccountID
	return inv, nil
}

// isMember 判断账号当前是否仍是项目成员。
func (s *Service) isMember(ctx context.Context, accountID, tenantID string) bool {
	members, err := s.store.MembershipsOf(ctx, accountID)
	if err != nil {
		return false
	}
	for i := range members {
		if members[i].TenantID == tenantID {
			return true
		}
	}
	return false
}

// switchTenant 把会话（或账号级）当前项目切到 tenantID。
func (s *Service) switchTenant(ctx context.Context, actor domain.Principal, tenantID string) error {
	if actor.SessionID != "" {
		if err := s.store.SetSessionTenant(ctx, actor.SessionID, tenantID); err != nil {
			return err
		}
	} else if err := s.store.SetAccountTenant(ctx, actor.AccountID, tenantID); err != nil {
		return err
	}
	s.invalidatePrincipalCache()
	return nil
}

func (s *Service) lookupInvite(ctx context.Context, rawToken string) (domain.ProjectInvite, error) {
	rawToken = strings.TrimSpace(rawToken)
	if rawToken == "" {
		return domain.ProjectInvite{}, inviteInvalid()
	}
	inv, err := s.store.GetInviteByHash(ctx, domain.HashInviteToken(rawToken))
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.ProjectInvite{}, inviteInvalid()
		}
		return domain.ProjectInvite{}, err
	}
	return inv, nil
}

// inviteInvalid 是所有失效情形的统一错误：不区分不存在、过期、撤销与他人已用。
func inviteInvalid() error {
	return domain.NotFound("invite", "邀请无效或已过期")
}
