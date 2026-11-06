// 包级说明见 doc.go。
package dashboard

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Tenants 是项目注册表端口，由 service/tenant 实现。命名、保留前缀、归档与停用规则
// 都在服务里，这里只做编解码与授权。
type Tenants interface {
	Get(ctx context.Context, id string) (domain.Tenant, error)
	List(ctx context.Context) ([]domain.Tenant, error)
	Create(ctx context.Context, actor domain.Principal, in tenant.CreateInput) (domain.Tenant, error)
	Update(ctx context.Context, id string, patch tenant.Patch) (domain.Tenant, error)
	// Disable 回收（停用）项目；个人项目不能停用。
	Disable(ctx context.Context, id string) error
	Archive(ctx context.Context, id string) error
	Restore(ctx context.Context, id string) error
}

// Tokens 是 Token 管理端口。
type Tokens interface {
	IssueToken(ctx context.Context, tenantID, label, createdBy string) (string, domain.Token, error)
	// IssueTokenWithExpiry 签发可过期 Token；in.ExpiresAt 为 nil 表示永不过期。
	IssueTokenWithExpiry(ctx context.Context, in identity.TokenIssueInput) (string, domain.Token, error)
	GetToken(ctx context.Context, id string) (domain.Token, error)
	ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
	RevokeToken(ctx context.Context, id string) error
	// RenameToken 只改展示名，不动哈希与过期时间。
	RenameToken(ctx context.Context, id, label string) error
}

// Accounts 是请求认证、会话级项目切换与成员写入端口，由 account 服务实现。
type Accounts interface {
	Authenticate(ctx context.Context, c domain.Credentials) (domain.Principal, error)
	SwitchProject(ctx context.Context, p domain.Principal, tenantID string) error
	// MemberRoles 返回账号在全部项目里的角色，包含停用/归档项目；归档项目
	// 不出现在 Principal.Roles 里，列表与恢复入口需要这份完整成员关系。
	MemberRoles(ctx context.Context, accountID string) (map[string]domain.Role, error)
}

// Memories 是上游记忆读取端口，由 adapter/upstream 实现。
type Memories interface {
	// Health 探测上游 /health，返回原始 JSON（/api/meta.health 直接给前端看）。
	Health(ctx context.Context) (map[string]any, error)
	OwnedMemory(ctx context.Context, tenantID, userID, memoryID string) (domain.Memory, bool, error)
	Dashboard(ctx context.Context, q domain.DashboardQuery) (domain.DashboardPage, error)
}

// Templates 是画像模板端口，由 adapter/upstream 实现；租户由 tenantID 限定，控制面不落库。
type Templates interface {
	ListProfileTemplates(ctx context.Context, tenantID string, page, size int) (domain.ProfileTemplatePage, error)
	GetProfileTemplate(ctx context.Context, tenantID, id string) (domain.ProfileTemplate, error)
	AddProfileTemplate(ctx context.Context, tenantID, name string, metadata map[string]any) (string, error)
	UpdateProfileTemplate(ctx context.Context, tenantID, id string, in domain.ProfileTemplateUpdate) (string, error)
	DeleteProfileTemplate(ctx context.Context, tenantID, id string) error
}

// Prompts 是项目级抽取 prompt 端口，由 adapter/upstream 实现。
// 上游是配置的唯一权威；控制面不落库，也不改上游源码。
type Prompts interface {
	GetExtractPrompt(ctx context.Context, tenantID string) (domain.ExtractPrompt, error)
	SaveExtractPrompt(ctx context.Context, tenantID, prompt string) error
	ResetExtractPrompt(ctx context.Context, tenantID string) (domain.ExtractPrompt, error)
	GenerateExtractPrompt(ctx context.Context, tenantID, description string) (string, error)
	DebugExtract(
		ctx context.Context,
		tenantID string,
		messages []domain.PromptMessage,
		types []string,
	) (domain.ExtractDebug, error)
}

// Knowledge 是知识库端口，由 adapter/upstream 实现（上游按 mem_cube_id 隔离）。
type Knowledge interface {
	KnowledgeBases(ctx context.Context, tenant string) ([]domain.KnowledgeBase, error)
	KnowledgeBase(ctx context.Context, tenant, id string) (domain.KnowledgeBase, error)
	CreateKnowledgeBase(ctx context.Context, tenant, name, description string) (domain.KnowledgeBase, error)
	UpdateKnowledgeBase(ctx context.Context, in domain.KnowledgeBaseUpdate) (domain.KnowledgeBase, error)
	DeleteKnowledgeBase(ctx context.Context, tenant, id string) error
	KnowledgeFiles(ctx context.Context, in domain.KnowledgeFilesQuery) (domain.KnowledgeFilePage, error)
	AddKnowledgeFiles(ctx context.Context, in domain.KnowledgeFilesAdd) ([]domain.KnowledgeFile, error)
	DeleteKnowledgeFiles(ctx context.Context, tenant string, fileIDs []string) (int, error)
	DownloadKnowledgeFile(ctx context.Context, tenant, fileID string) ([]byte, string, error)
}

// Models 是上游模型清单端口，由 adapter/upstream 实现。
type Models interface {
	Models(ctx context.Context) ([]domain.ModelInfo, error)
}

// Graph 是记忆图谱端口，由 adapter/upstream 实现（memos-api 的 /product/get_memory_graph 等）。
type Graph interface {
	// Ping 探测上游连通性，用于 /api/meta 的数据源状态。
	Ping(ctx context.Context) error
	EventMemories(ctx context.Context, in domain.EventMemoriesQuery) ([]domain.GraphMemory, int, error)
	GraphMemories(ctx context.Context, tenant string, types []string, limit int) (domain.GraphPage, error)
	MemoryBody(ctx context.Context, tenant, id string) (domain.GraphMemory, error)
}

// /api/meta 的 authMode 取值，前端据此区分控制台登录与外部身份。
const (
	AuthModePage     = "page"
	AuthModeExternal = "external"
)

// Deps 是 Dashboard 路由的依赖。
type Deps struct {
	Tenants   Tenants
	Tokens    Tokens
	Memory    *memory.Service
	Memories  Memories
	Templates Templates
	Prompts   Prompts
	// PromptStudio 是提示词工作台（prompts/* 八条）；nil 时这些路由返回 503。
	PromptStudio PromptStudio
	Models       Models
	Accounts     Accounts
	Knowledge    Knowledge
	Audit        *audit.Recorder
	Graph        Graph
	Service      string
	// LocalSync 是本地插件记忆镜像；能力未装配时为 nil，相关路由显式 503。
	LocalSync LocalSync
	// AuthMode 是 /api/meta 的 authMode；空值按 page 处理。
	AuthMode string
	// GatewayURL 是本进程 MCP 入口的集群内地址（如 http://127.0.0.1:8002），
	// /api/health 用它探测网关；为空表示本角色没有同进程的 MCP。
	GatewayURL string
	// GraphMaxNodes 是控制台图谱单次返回的节点上限，也作为请求里 limit 的上限。
	GraphMaxNodes int
	// WebDir 是前端产物目录（通常是 /app/web）。启动时读其中的 frontend.json，
	// 把「这份前端是哪次提交」经 /api/meta 报出去；文件不存在就留空。
	WebDir string
	Logger *slog.Logger
}

// Server 提供 Dashboard 路由。
type Server struct {
	deps Deps
	mux  *http.ServeMux
	// frontend 是构建时写进 web/dist/frontend.json 的前端身份（分支/提交/是否 dirty）。
	// 前端在另一个仓库，镜像里只有产物；不读这份文件就只能靠比对 asset hash 猜版本。
	frontend map[string]any
	// healthHTTP 探测同进程网关，复用连接：页面每 30s 轮询一次，没必要每轮新建 Transport。
	healthHTTP *http.Client
	// routes 记录注册过的 "METHOD /path" 模式，供 openapi 契约测试做双向断言。
	routes []string
}

// New 构造 Dashboard 路由；缺失必需依赖时返回错误。
func New(deps Deps) (*Server, error) {
	hasRepoDeps := deps.Tenants != nil && deps.Tokens != nil && deps.Accounts != nil
	hasDataDeps := deps.Memory != nil && deps.Knowledge != nil
	if !hasRepoDeps || !hasDataDeps {
		return nil, errors.New("dashboard: Tenants/Tokens/Accounts/Memory/Knowledge 不能为空")
	}
	if deps.GraphMaxNodes <= 0 {
		return nil, errors.New("dashboard: GraphMaxNodes 必须为正数")
	}
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	if deps.AuthMode == "" {
		deps.AuthMode = AuthModePage
	}
	s := &Server{
		deps:       deps,
		mux:        http.NewServeMux(),
		frontend:   readFrontendMeta(deps.WebDir),
		healthHTTP: &http.Client{Timeout: healthProbeTimeout},
	}
	s.register()
	// 未知路径也回 JSON 信封；Go 1.22 会优先匹配更具体的 pattern。
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		httpx.NotFoundJSON(w)
	})
	return s, nil
}

// Handler 返回挂到 console 角色的处理器。
func (s *Server) Handler() http.Handler { return httpx.MethodNotAllowedJSON(s.mux) }

type handler func(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error)

func (s *Server) register() {
	const base = "/api/dashboard/"
	// 只有变更类操作写审计（文档口径）；读接口不落审计，避免自动刷新把审计表刷满。
	route := func(pattern, tool string, rule auth.Rule, fn handler) {
		s.routes = append(s.routes, pattern)
		s.mux.HandleFunc(pattern, s.wrap(tool, rule, fn, auditPolicy{}))
	}
	routeWrite := func(pattern, tool string, rule auth.Rule, fn handler) {
		s.routes = append(s.routes, pattern)
		s.mux.HandleFunc(pattern, s.wrap(tool, rule, fn, auditPolicy{enabled: true}))
	}
	// routeWritePrompt 用于提示词工作台：审计只记元数据，不记正文与试跑输入输出。
	routeWritePrompt := func(pattern, tool string, rule auth.Rule, fn handler, policy auditPolicy) {
		s.routes = append(s.routes, pattern)
		policy.enabled = true
		s.mux.HandleFunc(pattern, s.wrap(tool, rule, fn, policy))
	}
	// 项目
	route("GET "+base+"project/dict", "dashboard.project.dict", auth.Authed, s.projectDict)
	route("POST "+base+"project/list", "dashboard.project.list", auth.Authed, s.projectList)
	routeWrite("POST "+base+"project/create", "dashboard.project.create", auth.AuthedWith(domain.CapProjectCreate), s.projectCreate)
	routeWrite("POST "+base+"project/update", "dashboard.project.update", auth.Authed, s.projectUpdate)
	routeWrite("POST "+base+"project/revoke/{id}", "dashboard.project.revoke", auth.InPath(domain.CapProjectManage), s.projectRevoke)
	routeWrite("POST "+base+"project/delete/{id}", "dashboard.project.delete", auth.InPath(domain.CapProjectManage), s.projectDelete)
	// 归档项目的成员仍有成员关系，但 Principal.Roles 会过滤停用项目；
	// 恢复的判权在 handler 内按完整成员关系做（平台管理员或 owner）。
	routeWrite("POST "+base+"project/restore/{id}", "dashboard.project.restore", auth.Authed, s.projectRestore)
	// 密钥
	route("POST "+base+"keys/list", "dashboard.keys.list", auth.InBody(domain.CapProjectView), s.keyList)
	routeWrite("POST "+base+"keys/create", "dashboard.keys.create", auth.InBodyStrict(domain.CapProjectWrite), s.keyCreate)
	routeWrite("POST "+base+"keys/revoke/{id}", "dashboard.keys.revoke", auth.OnKey(domain.CapProjectWrite), s.keyRevoke)
	routeWrite("POST "+base+"keys/rename/{id}", "dashboard.keys.rename", auth.OnKey(domain.CapProjectWrite), s.keyRename)
	route("POST "+base+"keys/copy", "dashboard.keys.copy", auth.Authed, s.keyCopy)
	// 记忆
	route("POST "+base+"project/memory/detail", "dashboard.memory.detail", auth.InBody(domain.CapProjectView), s.memoryDetail)
	route("POST "+base+"project/memory/events", "dashboard.memory.events", auth.InBodyStrict(domain.CapProjectView), s.memoryEvents)
	route("POST "+base+"project/memory/activity", "dashboard.memory.activity", auth.InBodyStrict(domain.CapProjectView), s.memoryActivity)
	routeWrite("POST "+base+"project/memory/delete", "dashboard.memory.delete", auth.InBodyStrict(domain.CapProjectWrite), s.memoryDelete)
	route("POST "+base+"project/memory/list", "dashboard.memory.list", auth.InBody(domain.CapProjectView), s.memoryList)
	// 日志与用量
	route("POST "+base+"log/list", "dashboard.log.list", auth.InBody(domain.CapProjectView), s.logList)
	route("POST "+base+"log/detail", "dashboard.log.detail", auth.Authed, s.logDetail)
	route("POST "+base+"usage/query", "dashboard.usage.query", auth.InBody(domain.CapProjectView), s.usageQuery)
	// Prompt：由上游按项目保存完整抽取模板（必须含 ${conversation}），控制面只代理。
	route("POST "+base+"prompt-config/detail", "dashboard.prompt.detail", auth.InBody(domain.CapProjectView), s.promptDetail)
	routeWrite("POST "+base+"prompt-config/save", "dashboard.prompt.save", auth.InBodyStrict(domain.CapProjectWrite), s.promptSave)
	routeWrite("POST "+base+"prompt-config/reset-default", "dashboard.prompt.reset", auth.InBodyStrict(domain.CapProjectWrite), s.promptReset)
	route("POST "+base+"prompt-config/generate", "dashboard.prompt.generate", auth.InBodyStrict(domain.CapProjectWrite), s.promptGenerate)
	route("POST "+base+"prompt-config/debug/extract", "dashboard.prompt.debug", auth.InBodyStrict(domain.CapProjectWrite), s.promptDebug)
	s.registerPromptStudio(base, route, routeWritePrompt)
	// 属性树模板
	route("POST "+base+"profileTemplate/list", "dashboard.profile.list", auth.InBody(domain.CapProjectView), s.profileList)
	route("GET "+base+"profileTemplate/detail", "dashboard.profile.detail", auth.InBody(domain.CapProjectView), s.profileDetail)
	routeWrite("POST "+base+"profileTemplate/create", "dashboard.profile.create", auth.InBodyStrict(domain.CapProjectWrite), s.profileCreate)
	routeWrite("POST "+base+"profileTemplate/update", "dashboard.profile.update", auth.InBodyStrict(domain.CapProjectWrite), s.profileUpdate)
	routeWrite("POST "+base+"profileTemplate/delete", "dashboard.profile.delete", auth.InBodyStrict(domain.CapProjectWrite), s.profileDelete)
	// 知识库：控制面只代理上游，按当前项目（mem_cube_id）隔离。
	kb := base + "knowledgebase/"
	route("POST "+kb+"list", "dashboard.kb.list", auth.InBody(domain.CapProjectView), s.kbList)
	route("POST "+kb+"detail", "dashboard.kb.detail", auth.InBody(domain.CapProjectView), s.kbDetail)
	route("POST "+kb+"file/list", "dashboard.kb.file.list", auth.InBody(domain.CapProjectView), s.kbFileList)
	route("POST "+kb+"memory/list", "dashboard.kb.memory.list", auth.InBody(domain.CapProjectView), kbMemoryUnavailable)
	routeWrite("POST "+kb+"create", "dashboard.kb.create", auth.InBodyStrict(domain.CapProjectWrite), s.kbCreate)
	routeWrite("POST "+kb+"update", "dashboard.kb.update", auth.InBodyStrict(domain.CapProjectWrite), s.kbUpdate)
	routeWrite("POST "+kb+"delete", "dashboard.kb.delete", auth.InBodyStrict(domain.CapProjectWrite), s.kbDelete)
	routeWrite("POST "+kb+"file/add", "dashboard.kb.file.add", auth.InBodyStrict(domain.CapProjectWrite), s.kbFileAdd)
	routeWrite("POST "+kb+"file/delete", "dashboard.kb.file.delete", auth.InBodyStrict(domain.CapProjectWrite), s.kbFileDelete)
	routeWrite("POST "+kb+"project/associate", "dashboard.kb.project.associate", auth.InBodyStrict(domain.CapProjectWrite), kbNoAssociation)
	routeWrite("POST "+kb+"project/remove", "dashboard.kb.project.remove", auth.InBodyStrict(domain.CapProjectWrite), kbNoAssociation)
	// 下载返回原始文件流，不走 JSON 信封与审计落库。
	s.routes = append(s.routes, "GET "+kb+"file/download")
	s.mux.HandleFunc("GET "+kb+"file/download", s.kbFileDownload)
	// 其余
	route("GET /api/meta", "dashboard.meta", auth.Public, s.meta)
	// 接口文档页的状态灯：页面每 30s 轮询一次，只做探测，不读库。
	route("GET /api/health", "dashboard.health", auth.Authed, s.health)
	route("GET /api/user/quota/summary", "dashboard.quota", auth.Authed, s.quota)
	route("POST /api/user/subscription/details", "dashboard.subscription", auth.Authed, s.subscription)
	route("GET "+base+"models", "dashboard.models", auth.Authed, s.models)
	// 图谱（BFF 扩展）
	route("GET "+base+"graph/memories", "dashboard.graph.memories", auth.InQuery(domain.CapProjectView), s.graphMemories)
	route("GET "+base+"graph/memory", "dashboard.graph.memory", auth.InQuery(domain.CapProjectView), s.graphMemory)
	// 本地插件记忆镜像。读操作 viewer，删除与转存 editor；
	// 变更类动作的审计由 localsync service 写，这里不重复。
	ls := base + "local-sync/"
	route("GET "+ls+"devices", "local_sync.device.list", auth.InQueryProject(domain.CapProjectView), s.localDevices)
	route("DELETE "+ls+"devices/{id}", "local_sync.device.revoke", auth.InQueryProject(domain.CapProjectWrite), s.localDeviceRevoke)
	route("GET "+ls+"memories", "local_sync.memory.list", auth.InQueryProject(domain.CapProjectView), s.localMemories)
	route("GET "+ls+"summary", "local_sync.summary", auth.InQueryProject(domain.CapProjectView), s.localMemorySummary)
	route("GET "+ls+"memories/{id}", "local_sync.memory.detail", auth.InQueryProject(domain.CapProjectView), s.localMemoryDetail)
	route("DELETE "+ls+"memories/{id}", "local_sync.memory.delete", auth.InQueryProject(domain.CapProjectWrite), s.localMemoryDelete)
	route("POST "+ls+"memories/promote", "local_sync.memory.promote", auth.InQueryProject(domain.CapProjectWrite), s.localMemoryPromote)
}

// routeFunc 是 register 里注册单条路由的形状，供按功能拆出的注册函数共用。
type routeFunc func(pattern, tool string, rule auth.Rule, fn handler)

// registerPromptStudio 注册提示词工作台（prompts/*）。
func (s *Server) registerPromptStudio(base string, route routeFunc, routeWritePrompt func(string, string, auth.Rule, handler, auditPolicy)) {
	// 提示词工作台（prompts/*）：注册表、版本与试跑都在上游，控制面只代理。
	// 读三条要 viewer，写五条要 editor；只有 save/reset 写审计，且只记元数据。
	ps := base + "prompts/"
	// spec 第 7 节：所有 prompts/* 都必须显式带 projectId。读路由若用 InBody，
	// 缺字段会静默回落到会话当前项目，读到的可能不是用户以为的那个项目。
	route("POST "+ps+"list", "dashboard.prompts.list", auth.InBodyStrict(domain.CapProjectView), s.promptsList)
	route("POST "+ps+"detail", "dashboard.prompts.detail", auth.InBodyStrict(domain.CapProjectView), s.promptsDetail)
	route("POST "+ps+"versions", "dashboard.prompts.versions", auth.InBodyStrict(domain.CapProjectView), s.promptsVersions)
	route("POST "+ps+"validate", "dashboard.prompts.validate", auth.InBodyStrict(domain.CapProjectWrite), s.promptsValidate)
	route("POST "+ps+"test", "dashboard.prompts.test", auth.InBodyStrict(domain.CapProjectWrite), s.promptsTest)
	route("POST "+ps+"generate", "dashboard.prompts.generate", auth.InBodyStrict(domain.CapProjectWrite), s.promptsGenerate)
	routeWritePrompt("POST "+ps+"save", "dashboard.prompts.save",
		auth.InBodyStrict(domain.CapProjectWrite), s.promptsSave,
		auditPolicy{redact: promptSaveAuditInput, annotate: promptSaveAuditAnnotate})
	routeWritePrompt("POST "+ps+"reset", "dashboard.prompts.reset",
		auth.InBodyStrict(domain.CapProjectWrite), s.promptsReset,
		auditPolicy{redact: promptResetAuditInput, annotate: promptResetAuditAnnotate})
}

// auditPolicy 控制一条变更类路由怎么写审计。
type auditPolicy struct {
	enabled bool
	// redact 在写审计前清洗入参；提示词正文与试跑输入输出不能进审计。
	redact func(map[string]any) map[string]any
	// annotate 成功后用响应补充元数据（例如新版本号），不参与业务判断。
	annotate func(input map[string]any, payload any)
}

func (s *Server) wrap(tool string, rule auth.Rule, fn handler, policy auditPolicy) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		trace := newAuditTrace(r.Context(), tool, policy.enabled)
		fail := func(ctx context.Context, err error) {
			status, _, _ := mapError(err)
			trace.fail(ctx, auditEvent{Server: s, Started: started, Err: err, Status: status})
			s.writeError(w, err)
		}
		p, err := rule.Resolve(r.Context(), s.deps.Accounts, r)
		if err != nil {
			fail(r.Context(), err)
			return
		}
		trace.setUser(p)
		body, err := decodeBody(r)
		if err != nil {
			fail(r.Context(), err)
			return
		}
		trace.setInput(body, policy.redact)
		tenantID, err := rule.Authorize(r.Context(), auth.AuthzInput{
			Principal: p,
			Request:   r,
			Body:      body,
			Keys:      s.deps.Tokens,
		})
		trace.setTenant(tenantID)
		if err != nil {
			fail(r.Context(), err)
			return
		}
		if rule.Scope == auth.ScopeBodyProject || rule.Scope == auth.ScopeQueryProject {
			// 处理器只信这里写回的、已经过授权的项目，不再读请求里自报的值。
			body["projectId"] = tenantID
		}
		payload, err := fn(r.Context(), p, r, body)
		if err != nil {
			fail(r.Context(), err)
			return
		}
		trace.success(r.Context(), s, started, payload, policy.annotate)
		httpx.OK(w, payload)
	}
}

// auditTrace 只在变更类路由上构造记录；未启用时所有方法都是空操作。
type auditTrace struct {
	enabled bool
	rec     domain.AuditRecord
}

func newAuditTrace(ctx context.Context, tool string, enabled bool) auditTrace {
	if !enabled {
		return auditTrace{}
	}
	return auditTrace{
		enabled: true,
		rec: domain.AuditRecord{
			ID:       domain.NewID(),
			CallID:   domain.NewID(),
			TS:       time.Now(),
			Tool:     tool,
			Source:   domain.AuditSourceDashboard,
			RemoteIP: httpx.ClientIPFrom(ctx),
		},
	}
}

func (t *auditTrace) setUser(p domain.Principal) {
	if t.enabled {
		t.rec.UserID = p.Actor()
	}
}

func (t *auditTrace) setInput(body map[string]any, redact func(map[string]any) map[string]any) {
	if !t.enabled {
		return
	}
	if redact != nil {
		body = redact(body)
	}
	t.rec.Input = body
}

func (t *auditTrace) setTenant(id string) {
	if t.enabled {
		t.rec.TenantID = id
	}
}

// auditEvent 是一次失败调用的审计入参。
type auditEvent struct {
	Server  *Server
	Started time.Time
	Err     error
	Status  int
}

func (t *auditTrace) fail(ctx context.Context, ev auditEvent) {
	if !t.enabled {
		return
	}
	t.rec.Ms = time.Since(ev.Started).Milliseconds()
	t.rec.Err = errorName(ev.Err)
	t.rec.ErrMessage = domain.SafeMessage(ev.Err)
	t.rec.HTTPStatus = ev.Status
	ev.Server.record(ctx, t.rec)
}

func (t *auditTrace) success(
	ctx context.Context, s *Server, started time.Time, payload any,
	annotate func(map[string]any, any),
) {
	if !t.enabled {
		return
	}
	t.rec.Ms = time.Since(started).Milliseconds()
	t.rec.OK = true
	t.rec.HTTPStatus = http.StatusOK
	if annotate != nil {
		annotate(t.rec.Input, payload)
	}
	s.record(ctx, t.rec)
}

func decodeBody(r *http.Request) (map[string]any, error) {
	body := map[string]any{}
	if r.Method != http.MethodPost {
		return body, nil
	}
	if err := httpx.RequireJSON(r); err != nil {
		return nil, err
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return nil, err
	}
	return body, nil
}

// record 只记录已认证主体的调用；匿名请求不写审计，避免未登录者灌满审计表。
func (s *Server) record(ctx context.Context, record domain.AuditRecord) {
	if s.deps.Audit != nil && record.UserID != "" {
		s.deps.Audit.Record(ctx, record)
	}
}

// writeError 映射错误并写信封。
//
// Retry-After 只由知道等待时长的错误写：提示词工作台的令牌桶与上游 busy 带
// 明确秒数。其余 dashboard 429 来自上游的泛化限流，控制面不知道恢复时间，
// 不写这个头——早先按登录失败窗口写会让客户端等一个无关的时长。
func (s *Server) writeError(w http.ResponseWriter, err error) {
	status, code, message := mapError(err)
	if wait, ok := retryAfterOf(err); ok {
		httpx.SetRetryAfter(w, wait)
	}
	httpx.FailData(w, status, code, message, errorData(err))
}

// retryAfterOf 取限流错误建议的等待时长。
func retryAfterOf(err error) (time.Duration, bool) {
	var busy *domain.BusyError
	if errors.As(err, &busy) && busy.Wait > 0 {
		return busy.Wait, true
	}
	return 0, false
}

// errorData 是错误信封里除 message 之外的业务数据。目前只有两种：
// 版本冲突给当前版本号，限流给等待秒数。
func errorData(err error) map[string]any {
	// 是不是版本冲突由错误类型判断，不看值：别人把槽位还原成默认后，
	// 当前版本就是 0（合法值）。用 >0 判断会在这种时候把字段整个丢掉。
	if conflict, ok := errors.AsType[*domain.VersionConflictError](err); ok {
		return map[string]any{
			"currentVersionId": conflict.CurrentVersionID,
			"currentVersionNo": conflict.CurrentVersionNo,
		}
	}
	if wait, ok := retryAfterOf(err); ok {
		seconds := int((wait + time.Second - 1) / time.Second)
		return map[string]any{"retryAfterSec": max(seconds, 1)}
	}
	var invalid *domain.InvalidPromptError
	if errors.As(err, &invalid) && len(invalid.Errors) > 0 {
		return map[string]any{"errors": promptIssuesToJSON(invalid.Errors)}
	}
	return nil
}

func mapError(err error) (int, int, string) {
	switch {
	case errors.Is(err, httpx.ErrBodyTooLarge):
		return http.StatusRequestEntityTooLarge, httpx.CodePayloadTooLarge, "请求体过大"
	case errors.Is(err, domain.ErrQuotaExceeded):
		return http.StatusRequestEntityTooLarge, httpx.CodePayloadTooLarge, "请求体或配额超限"
	case errors.Is(err, httpx.ErrNotJSON):
		return http.StatusUnsupportedMediaType, httpx.CodeBadRequest, "Content-Type 必须是 application/json"
	case errors.Is(err, httpx.ErrBadJSON):
		return http.StatusBadRequest, httpx.CodeBadRequest, "请求体不是合法 JSON"
	case errors.Is(err, domain.ErrInvalidInput):
		return http.StatusBadRequest, httpx.CodeBadRequest, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrTooLarge):
		return http.StatusRequestEntityTooLarge, httpx.CodePayloadTooLarge, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrUnauthorized):
		return http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrForbidden):
		return http.StatusForbidden, httpx.CodeForbidden, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrNotFound):
		return http.StatusNotFound, httpx.CodeNotFound, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrRateLimited):
		return http.StatusTooManyRequests, httpx.CodeRateLimited, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrConflict):
		return http.StatusConflict, httpx.CodeConflict, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrModelFailed):
		return http.StatusBadGateway, httpx.CodeModelFailed, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrModelTimeout):
		return http.StatusGatewayTimeout, httpx.CodeModelTimeout, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrUpstreamRejected), errors.Is(err, domain.ErrUpstreamDenied):
		return http.StatusBadGateway, httpx.CodeUpstreamRejected, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrUpstreamUnavailable), errors.Is(err, domain.ErrDisabled):
		return http.StatusServiceUnavailable, httpx.CodeUnavailable, domain.SafeMessage(err)
	default:
		return http.StatusInternalServerError, httpx.CodeInternal, "内部错误"
	}
}

func errorName(err error) string {
	if kind := domain.KindOf(err); kind != nil {
		return kind.Error()
	}
	return "internal"
}

// maxPageNum 是控制台分页上限。列表接口用 OFFSET 分页，页码过深时 SQLite 要先
// 扫过前面的行；10k × 200 条已经远超中小规模部署的数据量，再深的页码按上限处理。
const maxPageNum = 10_000

func pageOf(body map[string]any) (int, int) {
	p := num.Clamp(httpx.Int(body["pageNum"], 1), 1, maxPageNum)
	size := num.Clamp(httpx.Int(body["pageSize"], 10), 1, 200)
	return p, size
}

func disabledCapability(name string) error {
	return domain.Wrap(domain.ErrDisabled, "dashboard", name+" 能力未启用", nil)
}
