package account

import (
	"context"
	"errors"
	"log/slog"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// personalSuffix 是个人的默认空间名后缀；用户之后可以改成任意名字。
const personalSuffix = " 的空间"

// ensurePersonalTenant 保证账号有一个个人项目，并把自己写成 owner。
// 幂等：已有的项目原样返回，不覆盖用户改过的名字与描述。
func (s *Service) ensurePersonalTenant(ctx context.Context, acct domain.Account) error {
	if acct.ID == "" {
		return nil
	}
	if _, err := s.store.PersonalTenantOf(ctx, acct.ID); err == nil {
		return nil
	} else if !errors.Is(err, domain.ErrNotFound) {
		return err
	}

	id := domain.PersonalTenantID(acct.ID)
	name := strings.TrimSpace(acct.DisplayName)
	if name == "" {
		name = acct.Username
	}
	now := s.now()
	tenant, err := s.store.EnsurePersonalTenant(ctx,
		domain.Tenant{
			ID:                 id,
			UserID:             id,
			Name:               name + personalSuffix,
			Kind:               domain.TenantKindPersonal,
			CreatedByAccountID: acct.ID,
			CreatedAt:          now,
		},
		domain.Membership{AccountID: acct.ID, TenantID: id, Role: domain.RoleOwner, CreatedAt: now},
	)
	if err != nil {
		return err
	}
	s.invalidatePrincipalCache()
	s.log.InfoContext(ctx, "personal project ensured",
		slog.String("account_id", acct.ID), slog.String("tenant_id", tenant.ID))
	return nil
}

// ensurePersonalTenants 在启动时为已有账号补建个人项目。单个账号失败只记 WARN，
// 不阻塞启动：登录或重新启用时会再走一次 ensurePersonalTenant。
func (s *Service) ensurePersonalTenants(ctx context.Context) {
	accounts, err := s.store.ListAccounts(ctx)
	if err != nil {
		s.log.WarnContext(ctx, "list accounts for personal projects failed", slog.Any("err", err))
		return
	}
	checked := 0
	for i := range accounts {
		acct := &accounts[i]
		if acct.Disabled {
			continue
		}
		if err := s.ensurePersonalTenant(ctx, *acct); err != nil {
			s.log.WarnContext(ctx, "ensure personal project failed",
				slog.String("account_id", acct.ID), slog.Any("err", err))
			continue
		}
		checked++
	}
	if checked > 0 {
		s.log.InfoContext(ctx, "personal projects checked", slog.Int("accounts", checked))
	}
}
