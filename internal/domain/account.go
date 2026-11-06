package domain

import "time"

// Role 是账号在某个项目空间内的角色，只增不减：viewer < editor < owner。
type Role string

const (
	RoleViewer Role = "viewer"
	RoleEditor Role = "editor"
	RoleOwner  Role = "owner"
)

func (r Role) rank() int {
	switch r {
	case RoleViewer:
		return 1
	case RoleEditor:
		return 2
	case RoleOwner:
		return 3
	default:
		return 0
	}
}

// Valid 表示角色是三个已知值之一。
func (r Role) Valid() bool { return r.rank() > 0 }

// AtLeast 表示 r 不低于 least。非法角色永远不满足。
func (r Role) AtLeast(least Role) bool {
	return r.rank() > 0 && r.rank() >= least.rank()
}

// Account 是登录控制台的人。PasswordHash 是 argon2id PHC 字符串，不含明文。
type Account struct {
	ID            string
	Username      string
	DisplayName   string
	Email         string
	PasswordHash  string
	PlatformAdmin bool
	Disabled      bool
	// CurrentTenantID 是账号级“当前项目空间”。会话客户端存在会话上；
	// 零信任等不建会话的外部身份只能存在这里，否则多项目用户无法切换。
	CurrentTenantID string
	CreatedAt       time.Time
	LastLoginAt     *time.Time
}

// Membership 是账号在一个项目空间里的角色。
type Membership struct {
	AccountID string
	TenantID  string
	Role      Role
	CreatedAt time.Time
}

// Session 是服务端会话。Hash 是会话 Token 的 SHA-256，明文只在 cookie 里。
type Session struct {
	ID              string
	Hash            [32]byte
	AccountID       string
	CurrentTenantID string
	CreatedAt       time.Time
	ExpiresAt       time.Time
	LastSeenAt      *time.Time
	RevokedAt       *time.Time
}

// ExternalIdentity 是可信网关（如零信任 Web 网关）认证通过后传递的用户身份。
// 它只说明“网关认定了这是谁”，不代表本系统已授权；账号映射与判权仍由 accounts 负责。
type ExternalIdentity struct {
	Source   string // 身份来源，例如 "zerotrust"
	Username string // 网关保证非空；大小写不敏感
	Name     string // 中文名，受 UUAP 半账号限制可能为空
	Email    string // 邮箱，可能为空
}

// Credentials 是一次请求带来的凭据：Authorization 头里的 Bearer、会话 cookie，
// 以及可信网关放进 context 的外部身份（由 transport 填充，请求方不可伪造）。
type Credentials struct {
	Bearer   string
	Session  string
	External *ExternalIdentity
}

// PrincipalKind 区分控制台请求的两种主体。
type PrincipalKind string

const (
	PrincipalAccount    PrincipalKind = "account"
	PrincipalAdminToken PrincipalKind = "admin_token"
)

// AdminTokenUsername 是管理 Token 主体在审计里的名字。
const AdminTokenUsername = "admin-token"

// Principal 是控制台请求的调用主体，只由服务端从凭据解析得到。
type Principal struct {
	Kind          PrincipalKind
	AccountID     string
	Username      string
	DisplayName   string
	SessionID     string
	TokenID       string
	PlatformAdmin bool
	CurrentTenant string
	Roles         map[string]Role // 项目 ID → 角色，不含平台管理员的隐含权限
}

// RoleIn 返回主体在项目里的有效角色：平台管理员对任意项目是 owner，非成员为空串。
func (p Principal) RoleIn(tenantID string) Role {
	if p.PlatformAdmin {
		return RoleOwner
	}
	return p.Roles[tenantID]
}

// MembershipIn 返回主体在项目里的真实成员角色。平台管理员的隐含权限不算成员：
// 权限判定用 RoleIn，界面展示（「所有者」徽标、预览普通用户视角）用这个，
// 避免把“管理员能看”说成“管理员是所有者”。
func (p Principal) MembershipIn(tenantID string) Role {
	return p.Roles[tenantID]
}

// Require 要求主体在项目里至少是 least；内部转成能力判定，保持单一真相。
// 消息不泄露项目是否存在。
func (p Principal) Require(tenantID string, least Role) error {
	if tenantID != "" && p.Can(tenantID, capabilityForRole(least)) {
		return nil
	}
	return Wrap(ErrForbidden, "acl", "没有该项目的操作权限", nil)
}

// RequirePlatformAdmin 要求主体是平台管理员。
func (p Principal) RequirePlatformAdmin() error {
	if p.PlatformAdmin {
		return nil
	}
	return Wrap(ErrForbidden, "acl", "需要平台管理员权限", nil)
}

// Actor 返回写入审计的主体名；未认证的主体为空串，调用方据此不写审计。
func (p Principal) Actor() string { return p.Username }

// MemberCandidate 是“按 @用户名 搜人”的结果；Member 表示是否已在目标项目。
type MemberCandidate struct {
	AccountID   string
	Username    string
	DisplayName string
	Disabled    bool
	Member      bool
}
