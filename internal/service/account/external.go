package account

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// externalForbidden 用于“身份可信但本系统不授权”的所有情形：账号未开通、被禁用、
// 不在白名单。统一消息避免枚举账号是否存在。
func externalForbidden() error {
	return domain.Wrap(domain.ErrForbidden, "authenticate", "账号未开通控制台权限，请联系管理员", nil)
}

// authenticateExternal 把可信网关的用户名映射成本地主体。不建会话、不发 cookie；
// 账号每请求查库（禁用立即生效），成员关系按账号缓存 10s。
func (s *Service) authenticateExternal(ctx context.Context, ext domain.ExternalIdentity) (domain.Principal, error) {
	name, err := normalizeUsername(ext.Username)
	if err != nil {
		return domain.Principal{}, unauthenticated()
	}
	if !s.externalAllowed(name) {
		return domain.Principal{}, externalForbidden()
	}

	acct, err := s.store.AccountByUsername(ctx, name)
	profileChanged := false
	switch {
	case err == nil:
		acct, profileChanged = s.syncExternalProfile(ctx, acct, ext)
	case errors.Is(err, domain.ErrNotFound):
		if !s.opts.External.AutoCreate {
			return domain.Principal{}, externalForbidden()
		}
		acct, err = s.createExternalAccount(ctx, name, ext)
		if err != nil {
			return domain.Principal{}, err
		}
	default:
		return domain.Principal{}, err
	}
	if acct.Disabled {
		return domain.Principal{}, externalForbidden()
	}

	// 账号状态每次都查库（禁用立即生效）；成员关系按账号缓存，避免每个请求
	// 都全表读项目再读成员。资料刚变过时不读旧缓存。
	key := externalCacheKey(acct.ID)
	if !profileChanged {
		if p, ok := s.cachedPrincipal(key); ok {
			return p, nil
		}
	}
	p, err := s.principalFor(ctx, acct, domain.Session{})
	if err != nil {
		return domain.Principal{}, err
	}
	// 外部身份没有会话，零值表示缓存只受 TTL 约束。
	s.cachePrincipal(key, p, time.Time{})
	return p, nil
}

func externalCacheKey(accountID string) [32]byte {
	return domain.HashToken("external:" + accountID)
}

// syncExternalProfile 用网关带来的中文名与邮箱补齐/刷新账号资料。网关数据变化不频繁，
// 只在值真的不同时写一次；写失败只告警，不影响本次认证（身份本身仍然有效）。
func (s *Service) syncExternalProfile(
	ctx context.Context,
	acct domain.Account,
	ext domain.ExternalIdentity,
) (domain.Account, bool) {
	name := strings.TrimSpace(ext.Name)
	email := strings.TrimSpace(ext.Email)
	changed := false
	if name != "" && name != acct.DisplayName {
		acct.DisplayName = name
		changed = true
	}
	if email != "" && email != acct.Email {
		acct.Email = email
		changed = true
	}
	if !changed {
		return acct, false
	}
	if err := s.store.SetAccountProfile(ctx, acct.ID, acct.DisplayName, acct.Email); err != nil {
		s.log.WarnContext(ctx, "sync external profile failed",
			slog.String("username", acct.Username), slog.Any("err", err))
	}
	return acct, true
}

func (s *Service) externalAllowed(name string) bool {
	if len(s.opts.External.AllowedUsers) == 0 {
		return true
	}
	return slices.Contains(s.opts.External.AllowedUsers, name)
}

// createExternalAccount 建一个没有口令的账号：口令登录对空哈希永远失败，
// 它只能通过网关身份进入。并发首登撞唯一键时回读既有账号。
func (s *Service) createExternalAccount(
	ctx context.Context,
	username string,
	ext domain.ExternalIdentity,
) (domain.Account, error) {
	display := strings.TrimSpace(ext.Name)
	if display == "" {
		display = username
	}
	acct := domain.Account{
		ID:          domain.NewID(),
		Username:    username,
		DisplayName: display,
		Email:       strings.TrimSpace(ext.Email),
		CreatedAt:   s.now(),
	}
	if err := s.store.CreateAccount(ctx, acct); err != nil {
		if !errors.Is(err, domain.ErrConflict) {
			return domain.Account{}, err
		}
		return s.adoptExistingAccount(ctx, username)
	}
	if err := s.ensurePersonalTenant(ctx, acct); err != nil {
		// 账号已建好；个人项目失败不能让首次登录直接失败，下次登录或重启会重试。
		s.log.WarnContext(ctx, "ensure personal project for external account failed",
			slog.String("account_id", acct.ID), slog.Any("err", err))
	}
	s.log.InfoContext(ctx, "external account created",
		slog.String("username", username),
		slog.String("source", ext.Source),
	)
	return acct, nil
}

// adoptExistingAccount 处理并发首登：账号已被另一个请求建好，复用它并补个人项目。
func (s *Service) adoptExistingAccount(ctx context.Context, username string) (domain.Account, error) {
	acct, err := s.store.AccountByUsername(ctx, username)
	if err != nil {
		return domain.Account{}, err
	}
	if err := s.ensurePersonalTenant(ctx, acct); err != nil {
		s.log.WarnContext(ctx, "ensure personal project after conflict failed",
			slog.String("account_id", acct.ID), slog.Any("err", err))
	}
	return acct, nil
}
