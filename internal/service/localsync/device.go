package localsync

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"sort"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const maxDeviceLabelRunes = 128

// DeviceCodeGrant 是 POST /api/local-sync/device/code 的响应。
type DeviceCodeGrant struct {
	DeviceCode      string
	UserCode        string
	VerificationURI string
	ExpiresIn       int
	Interval        int
}

// DeviceTokenGrant 是授权完成后轮询拿到的设备令牌，明文只出现这一次。
type DeviceTokenGrant struct {
	DeviceToken string
	DeviceID    string
	TenantID    string
	ProjectName string
}

// DeviceAuthorizePreview 是浏览器授权页展示的设备信息。
type DeviceAuthorizePreview struct {
	UserCode    string
	DeviceLabel string
	AgentKind   string
	ExpiresAt   time.Time
}

// ApproveInput 是浏览器确认授权页的提交内容。
type ApproveInput struct {
	UserCode string
	TenantID string
	Label    string
	Allow    bool
}

// CreateDeviceCode 生成设备授权码。匿名入口按来源 IP 限流，避免被刷满授权码表。
func (s *Service) CreateDeviceCode(ctx context.Context, clientIP, deviceLabel, agentKind string) (DeviceCodeGrant, error) {
	if !s.codeCalls.allow(clientIP) {
		return DeviceCodeGrant{}, domain.Wrap(domain.ErrRateLimited, "local_sync", "请求过于频繁，请稍后再试", nil)
	}
	deviceLabel = domain.TruncateRunes(trimmed(deviceLabel), maxDeviceLabelRunes)
	agentKind = domain.TruncateRunes(trimmed(agentKind), maxDeviceLabelRunes)

	now := s.now()
	expires := now.Add(s.opts.DeviceCodeTTL)
	var rawCode, userCode string
	for range 5 {
		code, err := newDeviceCode()
		if err != nil {
			return DeviceCodeGrant{}, err
		}
		uc, err := domain.NewDeviceUserCode()
		if err != nil {
			return DeviceCodeGrant{}, err
		}
		rawCode, userCode = code, uc
		err = s.store.CreateDeviceCode(ctx, domain.LocalDeviceCode{
			CodeHash:    domain.HashLocalSyncToken(code),
			UserCode:    uc,
			DeviceLabel: deviceLabel,
			AgentKind:   agentKind,
			Status:      domain.LocalCodePending,
			ExpiresAt:   expires,
			CreatedAt:   now,
		})
		if err == nil {
			break
		}
		if !errors.Is(err, domain.ErrConflict) {
			return DeviceCodeGrant{}, err
		}
		rawCode = ""
	}
	if rawCode == "" {
		return DeviceCodeGrant{}, domain.Wrap(domain.ErrConflict, "local_sync", "授权码生成冲突，请稍后重试", nil)
	}

	s.record(ctx, auditRecord("local_sync.device.code", "", "", map[string]any{
		"device_label": deviceLabel,
		"agent_kind":   agentKind,
	}, nil))
	return DeviceCodeGrant{
		DeviceCode:      rawCode,
		UserCode:        userCode,
		VerificationURI: s.opts.VerificationURI,
		ExpiresIn:       int(s.opts.DeviceCodeTTL.Seconds()),
		Interval:        int(s.opts.PollInterval.Seconds()),
	}, nil
}

// PollDeviceToken 轮询授权结果。pending 返回 ErrAuthorizationPending，
// 批准后发放设备令牌并消费授权码；拒绝或过期分别返回 ErrAccessDenied/ErrExpiredToken。
func (s *Service) PollDeviceToken(ctx context.Context, deviceCode string) (DeviceTokenGrant, error) {
	deviceCode = trimmed(deviceCode)
	if deviceCode == "" {
		return DeviceTokenGrant{}, ErrExpiredToken
	}
	code, err := s.store.DeviceCodeByHash(ctx, domain.HashLocalSyncToken(deviceCode))
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return DeviceTokenGrant{}, ErrExpiredToken
		}
		return DeviceTokenGrant{}, err
	}
	now := s.now()
	if !now.Before(code.ExpiresAt) {
		return DeviceTokenGrant{}, ErrExpiredToken
	}
	switch code.Status {
	case domain.LocalCodePending:
		return DeviceTokenGrant{}, ErrAuthorizationPending
	case domain.LocalCodeDenied, domain.LocalCodeConsumed:
		return DeviceTokenGrant{}, ErrAccessDenied
	case domain.LocalCodeApproved:
		return s.issueDevice(ctx, code, now)
	default:
		return DeviceTokenGrant{}, ErrExpiredToken
	}
}

func (s *Service) issueDevice(ctx context.Context, code domain.LocalDeviceCode, now time.Time) (DeviceTokenGrant, error) {
	if code.AccountID == "" || code.TenantID == "" {
		return DeviceTokenGrant{}, ErrAccessDenied
	}
	tenant, err := s.store.GetTenant(ctx, code.TenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return DeviceTokenGrant{}, ErrAccessDenied
		}
		return DeviceTokenGrant{}, err
	}
	if tenant.Disabled {
		return DeviceTokenGrant{}, ErrAccessDenied
	}
	raw, err := domain.NewLocalSyncToken()
	if err != nil {
		return DeviceTokenGrant{}, err
	}
	device := domain.LocalDevice{
		ID:        domain.NewID(),
		AccountID: code.AccountID,
		TenantID:  code.TenantID,
		DeviceID:  domain.NewLocalDeviceID(),
		Label:     code.DeviceLabel,
		AgentKind: code.AgentKind,
		TokenHash: domain.HashLocalSyncToken(raw),
		Status:    domain.LocalDeviceActive,
		CreatedAt: now,
	}
	if err := s.store.ConsumeDeviceCode(ctx, code.CodeHash, device, now); err != nil {
		if errors.Is(err, domain.ErrConflict) {
			return DeviceTokenGrant{}, ErrAccessDenied
		}
		return DeviceTokenGrant{}, err
	}
	name := tenant.Name
	if name == "" {
		name = tenant.ID
	}
	return DeviceTokenGrant{
		DeviceToken: raw,
		DeviceID:    device.DeviceID,
		TenantID:    device.TenantID,
		ProjectName: name,
	}, nil
}

// PreviewDeviceAuthorize 给浏览器授权页读出设备信息；不消费授权码。
func (s *Service) PreviewDeviceAuthorize(ctx context.Context, userCode string) (DeviceAuthorizePreview, error) {
	code, err := s.pendingCode(ctx, userCode)
	if err != nil {
		return DeviceAuthorizePreview{}, err
	}
	return DeviceAuthorizePreview{
		UserCode:    code.UserCode,
		DeviceLabel: code.DeviceLabel,
		AgentKind:   code.AgentKind,
		ExpiresAt:   code.ExpiresAt,
	}, nil
}

// ApproveDeviceCode 由登录账号批准或拒绝授权码。只接受真实成员关系，
// 因为设备之后的每次请求都会重新查 project_members。
func (s *Service) ApproveDeviceCode(ctx context.Context, p domain.Principal, in ApproveInput) error {
	code, err := s.pendingCode(ctx, in.UserCode)
	if err != nil {
		return err
	}
	if p.AccountID == "" {
		return domain.Wrap(domain.ErrUnauthorized, "local_sync", "设备授权需要控制台账号登录", nil)
	}
	now := s.now()
	label := domain.TruncateRunes(trimmed(in.Label), maxDeviceLabelRunes)
	if !in.Allow {
		err = s.store.DenyDeviceCode(ctx, code.CodeHash, now)
		s.record(ctx, auditRecord("local_sync.device.authorize", "", p.Username, map[string]any{
			"allow": false,
		}, err))
		return err
	}

	tenantID := trimmed(in.TenantID)
	if tenantID == "" {
		return domain.Invalid("local_sync", "缺少 tenantId")
	}
	// 直接查成员表而不是 p.Roles：平台管理员的 Roles 按设计是空的（隐含权限不落表），
	// 用映射会把“平台管理员 + 项目成员”误判成无权限；同时成员关系以库里当前值为准，
	// 与每次上传时 AuthenticateDevice 的复核口径保持一致。
	member, err := s.store.GetMembership(ctx, p.AccountID, tenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return forbidden()
		}
		return err
	}
	if !member.Role.Can(domain.CapLocalSync) {
		return forbidden()
	}
	tenant, err := s.store.GetTenant(ctx, tenantID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return forbidden()
		}
		return err
	}
	if tenant.Disabled {
		return forbidden()
	}
	if err := s.store.ApproveDeviceCode(ctx, code.CodeHash, p.AccountID, tenantID, label, now); err != nil {
		return err
	}
	s.record(ctx, auditRecord("local_sync.device.authorize", tenantID, p.Username, map[string]any{
		"device_label": label, "allow": true,
	}, nil))
	return nil
}

// ProjectOption 是授权页可选的一个项目，Role 只用于展示。
type ProjectOption struct {
	ID   string
	Name string
	Role domain.Role
}

// AuthorizableProjects 返回账号可以作为设备上传目标的项目：项目未停用，且当前
// 成员角色达到 editor。判权直接查成员表而不是 Principal.Roles —— 平台管理员的
// Roles 按设计恒为空，用映射会让“既是平台管理员又是项目成员”的账号在授权页
// 一个项目都看不到；这里的口径与 ApproveDeviceCode、AuthenticateDevice 一致。
func (s *Service) AuthorizableProjects(ctx context.Context, accountID string) ([]ProjectOption, error) {
	if strings.TrimSpace(accountID) == "" {
		return nil, unauthorized()
	}
	tenants, err := s.store.ListTenants(ctx)
	if err != nil {
		return nil, err
	}
	members, err := s.store.MembershipsOf(ctx, accountID)
	if err != nil {
		return nil, err
	}
	roles := make(map[string]domain.Role, len(members))
	for _, m := range members {
		roles[m.TenantID] = m.Role
	}
	out := make([]ProjectOption, 0, len(tenants))
	for i := range tenants {
		t := tenants[i]
		if t.Disabled || !roles[t.ID].Can(domain.CapLocalSync) {
			continue
		}
		name := t.Name
		if name == "" {
			name = t.ID
		}
		out = append(out, ProjectOption{ID: t.ID, Name: name, Role: roles[t.ID]})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

func (s *Service) pendingCode(ctx context.Context, userCode string) (domain.LocalDeviceCode, error) {
	normalized := domain.NormalizeUserCode(userCode)
	if normalized == "" {
		return domain.LocalDeviceCode{}, domain.NotFound("local_sync", "授权码不存在或已过期")
	}
	code, err := s.store.DeviceCodeByUserCode(ctx, normalized)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.LocalDeviceCode{}, domain.NotFound("local_sync", "授权码不存在或已过期")
		}
		return domain.LocalDeviceCode{}, err
	}
	if !s.now().Before(code.ExpiresAt) || code.Status != domain.LocalCodePending {
		return domain.LocalDeviceCode{}, domain.NotFound("local_sync", "授权码不存在或已过期")
	}
	return code, nil
}

func newDeviceCode() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", domain.Wrap(domain.ErrInvalidInput, "local_sync", "无法生成授权码", err)
	}
	return hex.EncodeToString(buf), nil
}
