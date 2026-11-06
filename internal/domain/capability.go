package domain

// Capability 是控制台授权的最小单位。路由与用例只声明能力，角色到能力的
// 映射集中在 Role.Can，避免每处再写一遍 role >= editor 这类比较。
type Capability string

const (
	// CapProjectView 查看项目与项目内的记忆、日志、画像。
	CapProjectView Capability = "project:view"
	// CapProjectWrite 写入、删除记忆，以及修改项目内的配置。
	CapProjectWrite Capability = "project:write"
	// CapProjectManage 管理成员、邀请与项目自身的生命周期。
	CapProjectManage Capability = "project:manage"
	// CapProjectCreate 新建项目；只要求已登录的账号主体。
	CapProjectCreate Capability = "project:create"
	// CapPlatformManage 跨项目的平台运维。
	CapPlatformManage Capability = "platform:manage"
	// CapLocalSync 是设备令牌能访问本地同步通道的能力，等价于项目 editor。
	// 它只对 /api/local-sync/* 生效，不能用于 MCP、REST、Dashboard 或 Admin。
	CapLocalSync Capability = "local_sync:write"
)

// Can 是角色到能力的唯一映射：viewer 只读，editor 可写，owner 还能管人。
func (r Role) Can(c Capability) bool {
	switch c {
	case CapProjectView:
		return r.AtLeast(RoleViewer)
	case CapProjectWrite:
		return r.AtLeast(RoleEditor)
	case CapProjectManage:
		return r.AtLeast(RoleOwner)
	case CapProjectCreate:
		return r.Valid()
	case CapLocalSync:
		return r.AtLeast(RoleEditor)
	case CapPlatformManage:
		return false
	}
	return false
}

// Can 判断主体在项目里的有效能力。CapProjectCreate 不依赖项目角色：任何已认证
// 主体都能调用建项目；管理 Token 没有账号身份，由 handler 要求 ownerUsername，
// 否则会留下无主项目。
func (p Principal) Can(tenantID string, c Capability) bool {
	if c == CapProjectCreate {
		return p.Kind == PrincipalAccount || p.Kind == PrincipalAdminToken
	}
	if p.PlatformAdmin {
		return true
	}
	return p.RoleIn(tenantID).Can(c)
}

// RequireCap 要求主体在项目里具备能力；消息不泄露项目是否存在。
func (p Principal) RequireCap(tenantID string, c Capability) error {
	if p.Can(tenantID, c) {
		return nil
	}
	return Wrap(ErrForbidden, "acl", "没有该项目的操作权限", nil)
}

// capabilityForRole 给角色式的旧入口兜底；新代码直接用能力表达。
func capabilityForRole(r Role) Capability {
	switch r {
	case RoleOwner:
		return CapProjectManage
	case RoleEditor:
		return CapProjectWrite
	default:
		return CapProjectView
	}
}
