package account

import (
	"context"
	"errors"
	"log/slog"
	"maps"
	"sort"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func unauthenticated() error {
	return domain.Wrap(domain.ErrUnauthorized, "authenticate", "登录已过期，请重新登录", nil)
}

// Authenticate 把一次请求的凭据解析成主体。Bearer 存在时只按管理 Token 解析；
// 其次是可信网关的外部身份；都没有时按会话 cookie 解析。
// External.Required 打开后，本地会话不再能单独通过。
func (s *Service) Authenticate(ctx context.Context, c domain.Credentials) (domain.Principal, error) {
	if c.Bearer != "" {
		return s.authenticateAdminToken(ctx, c.Bearer)
	}
	if c.External != nil && s.opts.External.Enabled {
		return s.authenticateExternal(ctx, *c.External)
	}
	if s.opts.External.Required {
		return domain.Principal{}, unauthenticated()
	}
	return s.authenticateSession(ctx, c.Session)
}

// authenticateSession 按会话 cookie 解析主体。
func (s *Service) authenticateSession(ctx context.Context, raw string) (domain.Principal, error) {
	if raw == "" {
		return domain.Principal{}, unauthenticated()
	}
	hash := domain.HashToken(raw)
	if p, ok := s.cachedPrincipal(hash); ok {
		s.touchSession(ctx, p.SessionID)
		return p, nil
	}
	sess, acct, err := s.liveSession(ctx, raw)
	if err != nil {
		return domain.Principal{}, err
	}
	p, err := s.principalFor(ctx, acct, sess)
	if err != nil {
		return domain.Principal{}, err
	}
	s.cachePrincipal(hash, p, sess.ExpiresAt)
	s.touchSession(ctx, sess.ID)
	return p, nil
}

func (s *Service) cachedPrincipal(hash [32]byte) (domain.Principal, bool) {
	now := s.now()
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	entry, ok := s.cache[hash]
	if !ok || now.After(entry.expires) {
		delete(s.cache, hash)
		return domain.Principal{}, false
	}
	// 零值表示没有会话（外部身份），只受缓存 TTL 约束。
	if !entry.sessionExpires.IsZero() && !now.Before(entry.sessionExpires) {
		delete(s.cache, hash)
		return domain.Principal{}, false
	}
	return clonePrincipal(entry.principal), true
}

func (s *Service) cachePrincipal(hash [32]byte, p domain.Principal, sessionExpires time.Time) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	if len(s.cache) >= 4096 {
		clear(s.cache)
	}
	s.cache[hash] = cachedPrincipal{
		principal:      clonePrincipal(p),
		expires:        s.now().Add(principalCacheTTL),
		sessionExpires: sessionExpires,
	}
}

// InvalidatePrincipalCache 清空全部会话认证缓存。租户启停等不经过本服务的写操作
// 也需要调用它，保证权限变更立即生效。
func (s *Service) InvalidatePrincipalCache() { s.invalidatePrincipalCache() }

// invalidatePrincipalCache 清空全部会话认证缓存；写操作后调用，保证下一个请求重新解析。
func (s *Service) invalidatePrincipalCache() {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	clear(s.cache)
}

// clonePrincipal 复制 Roles，避免调用方修改缓存里的映射。
func clonePrincipal(p domain.Principal) domain.Principal {
	if p.Roles != nil {
		roles := make(map[string]domain.Role, len(p.Roles))
		maps.Copy(roles, p.Roles)
		p.Roles = roles
	}
	return p
}

func (s *Service) authenticateAdminToken(ctx context.Context, bearer string) (domain.Principal, error) {
	if s.admin == nil {
		return domain.Principal{}, unauthenticated()
	}
	caller, err := s.admin.AuthenticateAdmin(ctx, bearer)
	if err != nil {
		return domain.Principal{}, err
	}
	return domain.Principal{
		Kind:          domain.PrincipalAdminToken,
		Username:      domain.AdminTokenUsername,
		TokenID:       caller.TokenID,
		PlatformAdmin: true,
	}, nil
}

func (s *Service) liveSession(ctx context.Context, raw string) (domain.Session, domain.Account, error) {
	sess, err := s.store.SessionByHash(ctx, domain.HashToken(raw))
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.Session{}, domain.Account{}, unauthenticated()
		}
		return domain.Session{}, domain.Account{}, err
	}
	if sess.RevokedAt != nil || !s.now().Before(sess.ExpiresAt) {
		return domain.Session{}, domain.Account{}, unauthenticated()
	}
	acct, err := s.store.GetAccount(ctx, sess.AccountID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.Session{}, domain.Account{}, unauthenticated()
		}
		return domain.Session{}, domain.Account{}, err
	}
	if acct.Disabled {
		return domain.Session{}, domain.Account{}, unauthenticated()
	}
	return sess, acct, nil
}

// principalFor 加载成员关系（只含未停用的项目空间）并确定当前项目。
func (s *Service) principalFor(
	ctx context.Context,
	acct domain.Account,
	sess domain.Session,
) (domain.Principal, error) {
	tenants, err := s.store.ListTenants(ctx)
	if err != nil {
		return domain.Principal{}, err
	}
	enabled := make(map[string]struct{}, len(tenants))
	for i := range tenants {
		if !tenants[i].Disabled {
			enabled[tenants[i].ID] = struct{}{}
		}
	}
	// 成员关系对平台管理员同样要加载：鉴权仍由 PlatformAdmin 放行（RoleIn
	// 对管理员恒为 owner），但项目列表要用它区分“真的加入了这个项目”和
	// “只是管理员能看”，否则界面会把管理员标成每个项目的所有者。
	roles := map[string]domain.Role{}
	members, err := s.store.MembershipsOf(ctx, acct.ID)
	if err != nil {
		return domain.Principal{}, err
	}
	for _, m := range members {
		if _, ok := enabled[m.TenantID]; ok && m.Role.Valid() {
			roles[m.TenantID] = m.Role
		}
	}
	p := domain.Principal{
		Kind:          domain.PrincipalAccount,
		AccountID:     acct.ID,
		Username:      acct.Username,
		DisplayName:   acct.DisplayName,
		SessionID:     sess.ID,
		PlatformAdmin: acct.PlatformAdmin,
		Roles:         roles,
	}
	p.CurrentTenant = currentTenant(p, currentTenantOf(acct, sess), enabled, domain.PersonalTenantID(acct.ID))
	return p, nil
}

// currentTenantOf 优先取会话保存的项目；外部身份没有会话，退回账号级偏好。
func currentTenantOf(acct domain.Account, sess domain.Session) string {
	if sess.ID != "" {
		return sess.CurrentTenantID
	}
	return acct.CurrentTenantID
}

// currentTenant 取会话保存的项目；它不可访问时优先回到个人项目（personalID），
// 再回退为可访问项目里 ID 最小者，没有则为空串。
func currentTenant(p domain.Principal, saved string, enabled map[string]struct{}, personalID string) string {
	accessible := func(id string) bool {
		if p.PlatformAdmin {
			_, ok := enabled[id]
			return ok
		}
		_, ok := p.Roles[id]
		return ok
	}
	if saved != "" && accessible(saved) {
		return saved
	}
	if personalID != "" && accessible(personalID) {
		return personalID
	}
	var ids []string
	if p.PlatformAdmin {
		for id := range enabled {
			ids = append(ids, id)
		}
	} else {
		for id := range p.Roles {
			ids = append(ids, id)
		}
	}
	if len(ids) == 0 {
		return ""
	}
	sort.Strings(ids)
	return ids[0]
}

// touchSession 最多每分钟更新一次 last_seen_at，并把过期时间滑到
// min(now+SessionTTL, created_at+SessionMaxLifetime)。失败只影响会话计时，不拒绝请求。
func (s *Service) touchSession(ctx context.Context, id string) {
	now := s.now()
	s.mu.Lock()
	if last, ok := s.touched[id]; ok && now.Sub(last) < s.touchEvery() {
		s.mu.Unlock()
		return
	}
	if len(s.touched) >= maxTouchTracked {
		s.touched = make(map[string]time.Time)
	}
	s.touched[id] = now
	s.mu.Unlock()

	sess, err := s.store.GetSession(ctx, id)
	if err != nil {
		s.log.WarnContext(ctx, "read session for touch failed", slog.Any("err", err))
		return
	}
	// 已吊销或已过期的会话不续期：滑动窗口不能成为复活通道。
	if sess.RevokedAt != nil || !now.Before(sess.ExpiresAt) {
		return
	}
	expiresAt := s.slideSessionExpiry(sess.CreatedAt, now)
	if err := s.store.ExtendSession(ctx, id, now, expiresAt); err != nil {
		s.log.WarnContext(ctx, "extend session failed", slog.Any("err", err))
	}
}

// touchEvery 是会话续期的节流间隔：默认 1m，但必须小于空闲 TTL 的一半，
// 否则 TTL 很短（例如 1m）时下一次续期机会正好落在过期点上，活跃用户也会被登出。
func (s *Service) touchEvery() time.Duration {
	every := touchInterval
	if half := s.opts.SessionTTL / 2; half > 0 && half < every {
		every = half
	}
	if every < time.Second {
		every = time.Second
	}
	return every
}

// slideSessionExpiry 返回滑动续期后的过期时间：空闲超时与绝对上限取较早者。
func (s *Service) slideSessionExpiry(createdAt, now time.Time) time.Time {
	idle := now.Add(s.opts.SessionTTL)
	absolute := createdAt.Add(s.opts.SessionMaxLifetime)
	if idle.After(absolute) {
		return absolute
	}
	return idle
}

// SwitchProject 修改当前项目空间：有会话写会话，外部身份写账号级偏好。
func (s *Service) SwitchProject(ctx context.Context, p domain.Principal, tenantID string) error {
	defer s.invalidatePrincipalCache()
	if !p.Can(tenantID, domain.CapProjectView) {
		return domain.Wrap(domain.ErrForbidden, "switch_project", "没有该项目的操作权限", nil)
	}
	tenant, err := s.store.GetTenant(ctx, tenantID)
	if err != nil {
		return err
	}
	if tenant.Archived() {
		return domain.Wrap(domain.ErrForbidden, "switch_project", "该项目空间已归档，请先恢复", nil)
	}
	if tenant.Disabled {
		return domain.Wrap(domain.ErrForbidden, "switch_project", "该项目空间已停用", nil)
	}
	switch {
	case p.SessionID != "":
		return s.store.SetSessionTenant(ctx, p.SessionID, tenantID)
	case p.AccountID != "":
		return s.store.SetAccountTenant(ctx, p.AccountID, tenantID)
	default:
		return domain.Invalid("switch_project", "当前凭据没有会话，无法切换项目")
	}
}
