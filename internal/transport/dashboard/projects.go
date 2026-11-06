package dashboard

import (
	"context"
	"log/slog"
	"net/http"
	"sort"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	pageutil "github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/page"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// visibleProjects 返回主体能看到的项目空间。dict（侧边栏）只看可用项目；
// list（项目页）额外带出已归档、已停用的项目，供 owner 回滚或重新启用。
// 停用项目不出现在 dict，因为它不能切换也不能访问数据，只该出现在带「启用」
// 入口的项目页；其余角色判定沿用 Authenticate 已经算好的 Roles。
//
// 个人空间不是「项目」：只给本人和真实成员看，平台管理员非成员也不例外。
// 把别人的个人空间列进「项目与成员」，既让人以为它是可管理的共享项目，又把
// 每个账号的用户名摊在管理员界面上；但被邀请/加入的成员必须能看到，否则兑换
// 邀请后会落在一个侧栏里找不到的项目上。
func (s *Server) visibleProjects(
	ctx context.Context,
	p domain.Principal,
	includeArchived bool,
) ([]domain.Tenant, map[string]domain.Role, error) {
	tenants, err := s.deps.Tenants.List(ctx)
	if err != nil {
		return nil, nil, err
	}
	// 归档/停用项目的成员关系仍在库里，但 Principal.Roles 会过滤掉停用项目，
	// 所以 list 需要单独读一次完整成员关系，用来给这两类项目回填真实角色。
	var memberRoles map[string]domain.Role
	if includeArchived && p.AccountID != "" {
		memberRoles, err = s.deps.Accounts.MemberRoles(ctx, p.AccountID)
		if err != nil {
			return nil, nil, err
		}
	}
	out := make([]domain.Tenant, 0, len(tenants))
	for i := range tenants {
		if tenants[i].IsPersonal() && !isOwnPersonal(tenants[i], p) &&
			p.MembershipIn(tenants[i].ID) == "" && memberRoles[tenants[i].ID] == "" {
			continue
		}
		if s.projectVisible(tenants[i], p, includeArchived, memberRoles) {
			out = append(out, tenants[i])
		}
	}
	// 自己的个人项目排在最前：新用户默认选中第一项时会落在自己的空间里。
	// 平台管理员能看到别人的个人项目，但那些不应抢在前面。已归档项目排到最后。
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Archived() != out[j].Archived() {
			return !out[i].Archived()
		}
		return isOwnPersonal(out[i], p) && !isOwnPersonal(out[j], p)
	})
	return out, memberRoles, nil
}

func (s *Server) projectVisible(
	t domain.Tenant, p domain.Principal, includeArchived bool, memberRoles map[string]domain.Role,
) bool {
	if t.Archived() && !includeArchived {
		return false
	}
	// 停用项目只在项目页（list）里给真实成员看；侧栏（dict）对所有主体都不返回，
	// 避免切到一个不能访问的项目上。平台管理员同样在项目页里启用它。
	if t.Disabled && !includeArchived {
		return false
	}
	if p.PlatformAdmin {
		return true
	}
	role := p.MembershipIn(t.ID)
	if role == "" && (t.Archived() || t.Disabled) {
		role = memberRoles[t.ID]
	}
	return role != ""
}

func isOwnPersonal(t domain.Tenant, p domain.Principal) bool {
	return t.IsPersonal() && p.AccountID != "" && t.CreatedByAccountID == p.AccountID
}

// switchStatus 是前端识别“当前项目”的方式：第一个 ENABLED。账号主体只有当前项目
// 返回 ENABLED，其余返回 DISABLED（不代表被停用，停用另带 disabled:true）。
// 管理 Token 没有“当前项目”的概念，沿用项目本身的启停状态。
//
// 不能按 SessionID 是否为空来判断：零信任外部身份也不建会话，但账号级偏好里
// 仍然记着当前项目。按会话判断会让所有项目都返回 ENABLED。
func switchStatus(p domain.Principal, t domain.Tenant) string {
	if p.Kind == domain.PrincipalAdminToken {
		return projectStatus(t)
	}
	if !t.Disabled && t.ID == p.CurrentTenant {
		return "ENABLED"
	}
	return "DISABLED"
}

func (s *Server) projectDict(ctx context.Context, p domain.Principal, _ *http.Request, _ map[string]any) (any, error) {
	tenants, _, err := s.visibleProjects(ctx, p, false)
	if err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(tenants))
	for i := range tenants {
		item := map[string]any{
			"id":     tenants[i].ID,
			"name":   projectName(tenants[i]),
			"status": switchStatus(p, tenants[i]),
			"kind":   tenantKind(tenants[i]),
			// member 表示真实成员关系（不含平台管理员的隐含权限），「预览普通
			// 用户视角」用它裁掉管理员本来靠身份才能看到的项目。
			"member": p.MembershipIn(tenants[i].ID) != "",
			// 平台管理员没有成员关系，role 为空，由页面按 admin 单独放行。
			"role": string(p.MembershipIn(tenants[i].ID)),
		}
		out = append(out, item)
	}
	return out, nil
}

func (s *Server) projectList(
	ctx context.Context,
	p domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	tenants, memberRoles, err := s.visibleProjects(ctx, p, true)
	if err != nil {
		return nil, err
	}
	page, size := pageOf(body)
	records := make([]map[string]any, 0, len(tenants))
	for i := range tenants {
		item := projectJSON(tenants[i])
		item["status"] = switchStatus(p, tenants[i])
		item["disabled"] = tenants[i].Disabled
		// role 只报真实成员关系：平台管理员对任意项目都有权限，但不是“所有者”。
		// 用 RoleIn 会把管理员标成每个项目的 owner，界面上看起来像越权。
		role := p.MembershipIn(tenants[i].ID)
		if role == "" && (tenants[i].Archived() || tenants[i].Disabled) {
			// 停用/归档项目不在 Principal.Roles 里，回填成员角色供前端决定是否
			// 显示「启用」或「恢复」。
			role = memberRoles[tenants[i].ID]
		}
		item["role"] = string(role)
		item["member"] = role != ""
		records = append(records, item)
	}
	return map[string]any{"records": pageutil.Slice(records, page, size), "total": len(records)}, nil
}

func (s *Server) projectCreate(
	ctx context.Context,
	p domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	created, err := s.deps.Tenants.Create(ctx, p, tenant.CreateInput{
		Name:          httpx.String(body["name"]),
		Description:   httpx.String(body["description"]),
		MultiView:     httpx.Bool(body["isMultiView"], false),
		OwnerUsername: httpx.String(body["ownerUsername"]),
	})
	if err != nil {
		return nil, err
	}
	out := projectJSON(created)
	// 创建者就是 owner；管理 Token 没有账号身份，真正 owner 由 ownerUsername
	// 指定，不能把调用者写成 owner。Create 已把创建者写进 CreatedByAccountID，
	// 不需要再查一次成员表。
	var role domain.Role
	if p.AccountID != "" && created.CreatedByAccountID == p.AccountID {
		role = domain.RoleOwner
	}
	out["role"] = string(role)
	out["member"] = role != ""
	return out, nil
}

// projectUpdate 同时承载前端的几种用法：只带 id 是读取，要求 viewer；
// status=ENABLED 恢复停用项目，要求 owner；current=true 切换当前项目，要求 viewer；
// 改名、停用、description/isMultiView 变更要求项目 owner。先授权再产生任何副作用，
// 授权失败的请求不会改动会话。归档、个人项目等状态规则由 tenant 服务判定。
func (s *Server) projectUpdate(
	ctx context.Context,
	p domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	patch, err := parseProjectUpdate(body)
	if err != nil {
		return nil, err
	}
	needed := domain.CapProjectView
	if patch.editing() {
		needed = domain.CapProjectManage
	}
	if err := s.authorizeProjectUpdate(ctx, p, patch, needed); err != nil {
		return nil, err
	}
	// 先读一次：启用、切换与编辑都要看项目的当前状态，且读取不产生副作用。
	current, err := s.deps.Tenants.Get(ctx, patch.id)
	if err != nil {
		return nil, err
	}
	// 启用与切换是两件事：status=ENABLED 只把被停用的项目恢复回来，不改变调用方
	// 当前在看的项目；要切换必须显式带上 current=true。以前两者共用同一个信号，
	// 结果是「点一下启用」和「侧栏切个项目」都会顺带把项目改成启用态。
	if patch.switching() && current.Disabled {
		if current, err = s.enableDisabledProject(ctx, p, current); err != nil {
			return nil, err
		}
	}
	if patch.selectCurrent {
		if err := current.EnsureSelectable(); err != nil {
			return nil, err
		}
		if err := s.deps.Accounts.SwitchProject(ctx, p, patch.id); err != nil {
			return nil, err
		}
		p.CurrentTenant = patch.id
	}
	if patch.editing() {
		if _, err := s.deps.Tenants.Update(ctx, patch.id, patch.servicePatch()); err != nil {
			return nil, err
		}
	}
	return s.projectView(ctx, p, patch.id)
}

// authorizeProjectUpdate 在产生副作用前判权。停用项目不在 Principal.Roles 里，
// 纯启用（status=ENABLED 且不改其它字段）先用完整成员关系放行到启用分支，
// 再由那里要求 owner；非成员仍然拿原来的 403，不泄露项目是否存在。
func (s *Server) authorizeProjectUpdate(
	ctx context.Context,
	p domain.Principal,
	patch projectPatch,
	needed domain.Capability,
) error {
	err := p.RequireCap(patch.id, needed)
	if err == nil {
		return nil
	}
	if !patch.switching() || patch.editing() {
		return err
	}
	role, roleErr := s.memberRole(ctx, p, patch.id)
	if roleErr != nil {
		return roleErr
	}
	if !role.AtLeast(domain.RoleViewer) {
		return err
	}
	return nil
}

// enableDisabledProject 重新启用停用项目；只有 owner（或平台管理员）可以。
func (s *Server) enableDisabledProject(
	ctx context.Context,
	p domain.Principal,
	current domain.Tenant,
) (domain.Tenant, error) {
	role, err := s.memberRole(ctx, p, current.ID)
	if err != nil {
		return domain.Tenant{}, err
	}
	if !role.AtLeast(domain.RoleOwner) {
		return domain.Tenant{}, domain.Wrap(domain.ErrForbidden, "acl", "没有该项目的操作权限", nil)
	}
	enabled := false
	return s.deps.Tenants.Update(ctx, current.ID, tenant.Patch{Disabled: &enabled})
}

// projectPatch 是 project/update 的解析结果。「字段是否出现」与「字段的值」分开存：
// 缺省表示不改，出现（哪怕是 null/0）表示要写。
type projectPatch struct {
	id          string
	name        string
	status      string
	description string
	hasDesc     bool
	multiView   bool
	hasMulti    bool
	// selectCurrent 表示「把当前会话切到这个项目」。与 status=ENABLED 分开：
	// 后者是恢复一个被停用的项目（管理动作），不该顺带改变调用方正在看的项目。
	selectCurrent bool
}

func parseProjectUpdate(body map[string]any) (projectPatch, error) {
	p := projectPatch{
		id:   strings.TrimSpace(httpx.String(body["id"])),
		name: strings.TrimSpace(httpx.String(body["name"])),
	}
	if p.id == "" {
		return projectPatch{}, domain.Invalid("dashboard", "缺少 id")
	}
	status, err := statusOf(body)
	if err != nil {
		return projectPatch{}, err
	}
	p.status = status
	if raw, present := body["description"]; present {
		p.description, p.hasDesc = httpx.String(raw), true
	}
	if raw, present := body["isMultiView"]; present {
		p.multiView, p.hasMulti = httpx.Bool(raw, false), true
	}
	p.selectCurrent = httpx.Bool(body["current"], false)
	return p, nil
}

func (p projectPatch) switching() bool { return p.status == "ENABLED" }

func (p projectPatch) editing() bool {
	changesLabel := p.name != "" || p.hasDesc || p.hasMulti
	return changesLabel || p.status == "DISABLED"
}

// servicePatch 把 project/update 里要落库的字段交给 tenant 服务。status=ENABLED 已在
// 调用方处理，这里只有 DISABLED 会带上停用。
func (p projectPatch) servicePatch() tenant.Patch {
	var out tenant.Patch
	if p.name != "" {
		out.Name = &p.name
	}
	if p.hasDesc {
		out.Description = &p.description
	}
	if p.hasMulti {
		out.MultiView = &p.multiView
	}
	if p.status == "DISABLED" {
		disabled := true
		out.Disabled = &disabled
	}
	return out
}

// statusOf 取 body.status：缺省或 null 为空串，其余必须是 ENABLED 或 DISABLED。
func statusOf(body map[string]any) (string, error) {
	raw, present := body["status"]
	if !present || raw == nil {
		return "", nil
	}
	if status, _ := raw.(string); status == "ENABLED" || status == "DISABLED" {
		return status, nil
	}
	return "", domain.Invalid("dashboard", "status 只能是 ENABLED 或 DISABLED")
}

func (s *Server) projectView(ctx context.Context, p domain.Principal, id string) (any, error) {
	stored, err := s.deps.Tenants.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	item := projectJSON(stored)
	item["status"] = switchStatus(p, stored)
	item["disabled"] = stored.Disabled
	role := p.MembershipIn(id)
	if role == "" && p.AccountID != "" {
		// 停用/归档项目不在 Principal.Roles 里；按真实成员关系回填，供前端显示
		// 「所有者」徽标与「启用/恢复」入口。平台管理员不是成员时仍留白。
		roles, rolesErr := s.deps.Accounts.MemberRoles(ctx, p.AccountID)
		if rolesErr != nil {
			logger := s.deps.Logger
			if logger == nil {
				logger = slog.Default()
			}
			logger.WarnContext(ctx, "member roles lookup failed",
				slog.String("tenant_id", id), slog.Any("err", rolesErr))
		} else {
			role = roles[id]
		}
	}
	item["role"] = string(role)
	item["member"] = role != ""
	return item, nil
}

// memberRole 返回主体在项目里的真实成员角色，包含已停用与已归档项目。
// Principal.Roles 只含未停用项目，停用项目的 owner 需要这条路径才能看到并
// 启用自己的项目。平台管理员在判权上等价于 owner，但展示角色仍用 MembershipIn。
func (s *Server) memberRole(ctx context.Context, p domain.Principal, tenantID string) (domain.Role, error) {
	if p.PlatformAdmin {
		return domain.RoleOwner, nil
	}
	if role := p.MembershipIn(tenantID); role != "" {
		return role, nil
	}
	if p.AccountID == "" {
		return "", nil
	}
	roles, err := s.deps.Accounts.MemberRoles(ctx, p.AccountID)
	if err != nil {
		return "", err
	}
	return roles[tenantID], nil
}

func (s *Server) projectRevoke(
	ctx context.Context,
	_ domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	if err := s.deps.Tenants.Disable(ctx, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

// projectDelete 归档团队项目（软删除）：成员、邀请与 API Key 全部保留，之后可以经
// project/restore 原样恢复。个人项目不能归档，由 tenant 服务拒绝。
func (s *Server) projectDelete(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	id := r.PathValue("id")
	if err := p.RequireCap(id, domain.CapProjectManage); err != nil {
		return nil, err
	}
	if err := s.deps.Tenants.Archive(ctx, id); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "id": id, "archived": true}, nil
}

// projectRestore 撤销归档。项目归档后成员关系仍在库里，但 Principal.Roles 会把
// 停用项目过滤掉，所以这里用完整成员关系判权：平台管理员或项目 owner 可以恢复。
// 恢复后项目回到启用状态，成员与 API Key 立即恢复可用。
func (s *Server) projectRestore(
	ctx context.Context,
	p domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	id := r.PathValue("id")
	role, err := s.memberRole(ctx, p, id)
	if err != nil {
		return nil, err
	}
	if !role.AtLeast(domain.RoleOwner) {
		return nil, domain.Wrap(domain.ErrForbidden, "dashboard", "只有项目所有者可以恢复归档项目", nil)
	}
	if err := s.deps.Tenants.Restore(ctx, id); err != nil {
		return nil, err
	}
	return s.projectView(ctx, p, id)
}

func projectJSON(t domain.Tenant) map[string]any {
	multiView := 0
	if t.MultiView {
		multiView = 1
	}
	out := map[string]any{
		"id":          t.ID,
		"name":        projectName(t),
		"description": t.Description,
		"status":      projectStatus(t),
		"createTime":  t.CreatedAt,
		"isMultiView": multiView,
		"kind":        tenantKind(t),
		"archived":    t.Archived(),
	}
	if t.Archived() {
		out["archivedAt"] = domain.FormatBeijing(*t.ArchivedAt)
	}
	return out
}

func tenantKind(t domain.Tenant) string {
	if t.Kind == "" {
		return string(domain.TenantKindTeam)
	}
	return string(t.Kind)
}

func projectName(t domain.Tenant) string {
	if t.Name != "" {
		return t.Name
	}
	return t.ID
}

func projectStatus(t domain.Tenant) string {
	if t.Disabled {
		return "DISABLED"
	}
	return "ENABLED"
}
