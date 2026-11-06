package domain

import (
	"regexp"
	"strings"
	"time"
)

// Tenant 是记忆空间：cube_id ≡ mem_cube_id ≡ 图库 user_name ≡ 项目 ≡ 租户。
type Tenant struct {
	ID          string
	UserID      string
	Name        string
	Description string
	MultiView   bool
	Disabled    bool
	// Kind 区分团队项目与个人空间；历史行在迁移后取 team。
	Kind TenantKind
	// CreatedByAccountID 是创建者账号；个人项目靠它做唯一约束与归属回查。
	CreatedByAccountID string
	CreatedAt          time.Time
	// ArchivedAt 非空表示项目已归档（软删除）。归档同时会置 Disabled，
	// 访问控制在既有“停用项目”路径上生效；恢复时两者一起清空。
	ArchivedAt *time.Time
}

// TenantKind 是项目的类型，只有两种。
type TenantKind string

const (
	TenantKindTeam     TenantKind = "team"
	TenantKindPersonal TenantKind = "personal"
)

// PersonalTenantPrefix 是个人项目 ID 的保留前缀：团队项目不得以它开头，
// 否则可能与某个账号的确定性个人空间 ID 撞车。
const PersonalTenantPrefix = "u-"

// PersonalTenantID 是个人项目的确定性 ID，同时用作上游 user_id。
// 账号 ID 由服务端生成，不含路径分隔符，拼出来的 ID 不会与普通项目冲突。
func PersonalTenantID(accountID string) string { return PersonalTenantPrefix + accountID }

var tenantIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// ValidTenantID 判断项目 ID 是否合法。它同时是路径片段与上游 user_id，所以只放行
// 字母、数字、点、下划线、连字符，长度 1..64。
func ValidTenantID(id string) bool { return tenantIDPattern.MatchString(id) }

// IsReservedTenantID 判断 ID 是否落在个人项目的保留前缀内。
func IsReservedTenantID(id string) bool { return strings.HasPrefix(id, PersonalTenantPrefix) }

// IsPersonal 表示这是账号的个人空间。
func (t Tenant) IsPersonal() bool { return t.Kind == TenantKindPersonal }

// Archived 表示项目已被软删除。归档项目的成员关系保留，用于回滚。
func (t Tenant) Archived() bool { return t.ArchivedAt != nil && !t.ArchivedAt.IsZero() }

// EnsureNotArchived 拒绝对归档项目做任何改动：归档项目只能先恢复，否则会在一个
// “已删除”的项目上留下看不出来的修改。
func (t Tenant) EnsureNotArchived() error {
	if t.Archived() {
		return Wrap(ErrConflict, "tenant", "项目已归档，请先恢复", nil)
	}
	return nil
}

// EnsureDisablable 拒绝停用个人项目：它是账号的默认落脚点，停用会让新用户再次
// 无家可归。确需停用时走 /admin/tenants，那是平台管理员的明确动作。
func (t Tenant) EnsureDisablable() error {
	if t.IsPersonal() {
		return Wrap(ErrConflict, "tenant", "个人项目不能停用", nil)
	}
	return nil
}

// EnsureArchivable 拒绝归档个人项目：归档后新会话无处落脚。
func (t Tenant) EnsureArchivable() error {
	if t.IsPersonal() {
		return Wrap(ErrConflict, "tenant", "个人项目不能删除", nil)
	}
	return nil
}

// EnsureSelectable 判断项目能否被设为会话的当前项目：归档要先恢复，停用要先启用。
func (t Tenant) EnsureSelectable() error {
	if err := t.EnsureNotArchived(); err != nil {
		return err
	}
	if t.Disabled {
		return Wrap(ErrForbidden, "switch_project", "该项目空间已停用，请先在项目列表里启用", nil)
	}
	return nil
}

// Caller 是当前请求的身份，只能由 Bearer token 解析得到，绝不来自工具参数。
type Caller struct {
	TenantID string
	UserID   string
	TokenID  string
	Admin    bool
}

// Validate 检查调用方是否可用。service 的每个用例都必须先确认 TenantID 非空。
func (c Caller) Validate() error {
	if c.Admin {
		return nil
	}
	if c.TenantID == "" || c.UserID == "" {
		return Wrap(ErrUnauthorized, "caller", "调用方身份不完整", nil)
	}
	return nil
}
