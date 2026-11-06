---
module: domain
packages:
  - internal/domain
status: implemented
---

# domain

实体、值对象与带类型的错误：租户、Token、调用方、记忆条目、检索条件、审计记录、异步任务、时间口径。它不依赖本项目任何其他包，也没有外部 SDK 依赖。

## 职责与边界

- 负责：跨层共享的纯数据类型、不变量校验函数（如 `NewToken` 的哈希与掩码）、`errors.Is` 可判定的错误哨兵。
- 不负责：持久化（store）、HTTP 编解码（transport）、上游字段映射（adapter/upstream）。领域类型原则上不携带对外协议的 `json` 标签；对外形状在 transport 层单独定义，避免上游字段变化污染领域。例外只有时间序列化（`UTCTime.MarshalJSON`）与 `AddResult.CallID` 这类随幂等缓存一起复用的内部字段。

## 依赖

- 只依赖标准库（`time`、`crypto/sha256`、`crypto/rand`、`encoding/hex`、`errors`）。
- 被 service、adapter、transport、worker 依赖。

## 契约

### 租户与身份

```go
type Tenant struct {
    ID          string    // cube_id ≡ mem_cube_id ≡ 图库 user_name
    UserID      string    // 上游 user_id
    Name        string
    Description string    // 控制台维护的项目描述，不参与上游调用
    MultiView   bool      // 控制台维护的多视图开关，对外回显 0/1
    Disabled    bool
    Kind        TenantKind // "team"（默认）| "personal"
    CreatedByAccountID string // 创建者账号；个人项目靠它唯一
    CreatedAt   time.Time
    ArchivedAt  *time.Time // 非空表示已归档（软删除）；归档同时置 Disabled
}

type TenantKind string
const (
    TenantKindTeam     TenantKind = "team"
    TenantKindPersonal TenantKind = "personal"
)

func PersonalTenantID(accountID string) string // "u-" + accountID；账号 ID 由服务端生成，不含路径分隔符

type Caller struct {
    TenantID string
    UserID   string
    TokenID  string
    Admin    bool
}

type TokenKind string
const (
    TokenKindTenant TokenKind = "tenant"
    TokenKindAdmin  TokenKind = "admin"
)

type Token struct {
    ID         string
    Kind       TokenKind
    TenantID   string     // admin token 为空
    UserID     string
    Hash       [32]byte   // SHA-256(raw)，鉴权查找用
    Prefix     string     // 原始 token 前 8 位，仅用于列表展示
    Raw        string     // 明文；迁移 0024 起为控制台「有写权限再复制一次」保存，历史行为空
    Label      string
    CreatedAt  time.Time
    LastUsedAt *time.Time
    RevokedAt  *time.Time
    ExpiresAt  *time.Time // nil 表示永不过期（静态 Token / 管理 Token）
    CreatedByAccountID string // 只用于审计；管理 Token 创建为空
}

func NewRawToken() (string, error)        // 32 字节 crypto/rand，hex 编码，前缀 "memos_"
func HashToken(raw string) [32]byte
func TokenPrefix(raw string) string       // 前 8 个字符
func MaskToken(raw string) string         // 前 4 + … + 后 4；空串返回 ""
func (t Token) Expired(now time.Time) bool // ExpiresAt 非 nil 且 now 不早于它
```

不变量：

- 明文只在签发响应里自动出现一次；`Raw` 是额外的可复制副本，只允许项目写权限通过 `keys/copy` 单把读取，列表与其它读取路径都不下发。历史 Key（迁移 `0024` 之前）`Raw` 为空，不能当成「明文是空串」。
- `TokenKindAdmin` 的 `TenantID` 必须为空；`TokenKindTenant` 的 `TenantID` 与 `UserID` 必须非空（`Validate`）。
- 哈希用 SHA-256 而不是 bcrypt：token 由 32 字节随机数生成、不可猜测，鉴权路径需要按哈希查库（确定性）；bcrypt 的逐条 salt 无法做索引查找。
- 个人项目的 `ID` 与 `UserID` 都是 `PersonalTenantID(accountID)`，所以上游图库按 `user_name` 隔离时天然一人一空间；普通项目的 ID 不允许以 `u-` 开头，避免被误判成个人项目。

### 账号、主体与能力

```go
type Role string
const (
    RoleViewer Role = "viewer"
    RoleEditor Role = "editor"
    RoleOwner  Role = "owner"
)
func (r Role) Valid() bool
func (r Role) AtLeast(least Role) bool

type Capability string
const (
    CapProjectView   Capability = "project:view"   // 查看项目与项目内的记忆/日志
    CapProjectWrite  Capability = "project:write"  // 写入、删除、切换配置
    CapProjectManage Capability = "project:manage" // 成员、邀请、改名、停用
    CapProjectCreate Capability = "project:create" // 新建项目
    CapPlatformManage Capability = "platform:manage" // 跨项目的平台运维
)

func (r Role) Can(c Capability) bool
func (p Principal) Can(tenantID string, c Capability) bool // 平台管理员对所有项目与平台能力为 true

type Principal struct {
    Kind          PrincipalKind // account | admin_token
    AccountID     string
    Username      string
    DisplayName   string
    SessionID     string
    TokenID       string
    PlatformAdmin bool
    CurrentTenant string
    Roles         map[string]Role // 项目 ID → 角色；不含平台管理员的隐含权限
}

func (p Principal) RoleIn(tenantID string) Role
func (p Principal) MembershipIn(tenantID string) Role // 真实成员角色；平台管理员不隐含 owner
func (p Principal) Require(tenantID string, least Role) error
func (p Principal) RequireCap(tenantID string, c Capability) error
func (p Principal) RequirePlatformAdmin() error
func (p Principal) Actor() string

// MemberCandidate 是“按 @用户名 搜人”的结果；Member 表示是否已在目标项目。
type MemberCandidate struct {
    AccountID   string
    Username    string
    DisplayName string
    Disabled    bool
    Member      bool
}
```

不变量：

- 角色是唯一真相；能力矩阵是一张常量表（`viewer` → view；`editor` → view+write；`owner` → view+write+manage），路由与 service 都只问 `Can`，不再各自写 `role >= editor` 的比较。
- `CapProjectCreate` 只要求“已认证”：账号与管理 Token 都能调用建项目接口；管理 Token 没有账号身份，由 handler 强制要求 `ownerUsername`（缺失返回 400），否则会留下无主项目。
- `CapPlatformManage` 等价于 `PlatformAdmin`；平台管理员对任意项目 `RoleIn` 返回 owner，因此不需要在每处再判一次。
- `Principal.Roles` 不含平台管理员的隐含权限；`RoleIn` 用于判权，`MembershipIn` 只返回真实成员关系，用于界面展示（角色列、预览普通用户视角）。
- `Principal.Can` 对 `CapProjectCreate` 只要求主体是账号或管理 Token，不要求任何项目角色。

### 邀请

```go
type ProjectInvite struct {
    ID                 string
    TenantID           string
    Role               Role
    TokenHash          [32]byte
    CreatedByAccountID string
    CreatedAt          time.Time
    ExpiresAt          time.Time
    UsedAt             *time.Time
    UsedByAccountID    string
    RevokedAt          *time.Time
}

type InviteStatus string
const (
    InviteActive  InviteStatus = "active"
    InviteUsed    InviteStatus = "used"
    InviteExpired InviteStatus = "expired"
    InviteRevoked InviteStatus = "revoked"
)

func (i ProjectInvite) Status(now time.Time) InviteStatus // revoked → used → expired → active
func NewInviteToken() (string, error)                     // "inv_" + 32 字节 crypto/rand 的 hex
func HashInviteToken(raw string) [32]byte                 // SHA-256
```

不变量：`ProjectInvite` 没有明文字段，只有哈希；`Role` 必须是三个已知值之一，`owner` 邀请的准入限制在 service（需要平台管理员），不在类型里。状态判定是纯函数，`now` 由调用方传入，便于测试与同一请求内的时间一致。


### 抽取 prompt 与模型清单

上游项目级配置的只读投影，控制面不落库：

```go
type ExtractPrompt struct {
    Prompt          string   // 完整模板，必须含 ${conversation}
    ExtractionTypes []string
    UpdatedAt       string
    Source          string   // "default" | "saved"
    IsDefault       bool
}

type PromptMessage struct {
    Role    string
    Content string
}

type ExtractDebugResult struct {
    Type    string
    Content string
}

type ExtractDebug struct {
    ExtractionTypes []string
    Results         []ExtractDebugResult
}

type ModelInfo struct {
    Role   string
    Model  string
    Spec   string
    Source string   // 上游环境变量键名
    Group  string   // "memory" | "chat"
}
```

### 图谱、知识库与画像模板

```go
type GraphMemory struct {
    ID, Content, Type, UserID, SessionID, CreatedAt string
    SourceIDs []string
    Truncated bool // 上游按 label_max 截断，正文需按 id 另取
}
type GraphEdge struct{ Source, Target, Kind string }
type GraphPage struct{ Nodes []GraphMemory; Edges []GraphEdge; Total int; Truncated bool }

// EventMemoriesQuery 是事件记忆分页查询的入参；Types 至少给一个。
type EventMemoriesQuery struct {
    TenantID string
    Types    []string
    Query    string
    Page     int
    Size     int
}

type KnowledgeBase struct {
    ID, Name, Description string
    FileCount int; TotalSize int64
    CreatedAt, UpdatedAt time.Time
}
type KnowledgeFile struct {
    ID, KnowledgeBaseID, Name, Type, Status string
    Size int64; SizeMB float64; CreatedAt time.Time
}
type KnowledgeFileInput struct{ Name, Content, Type string } // Content 为 URL/data URL/裸 base64

// 知识库端口入参：改名（nil 表示不改）、分页查询、批量添加。
type KnowledgeBaseUpdate struct {
    TenantID    string
    ID          string
    Name        *string
    Description *string
}
type KnowledgeFilesQuery struct {
    TenantID        string
    KnowledgeBaseID string
    Page            int
    Size            int
}
type KnowledgeFilesAdd struct {
    TenantID        string
    KnowledgeBaseID string
    Files           []KnowledgeFileInput
    FileType        string
}

type ProfileTemplate struct { /* 身份字段 + 属性树；由 upstream 解码 */ }
type ProfileTemplateUpdate struct{ Name *string; Metadata map[string]any }
```

这些类型都是上游形状在控制面的投影，控制面不落库；租户条件由 `upstream` 显式带 `mem_cube_id`。`GraphMemory.CreatedAt` 是字符串：上游历史脏值只影响展示，不阻断正文读取。

### 记忆

```go
type MemoryType string
const (
    MemoryTypeText  MemoryType = "text"
    MemoryTypePref  MemoryType = "pref"
    MemoryTypeTool  MemoryType = "tool"
    MemoryTypeDream MemoryType = "dream"
)

type Memory struct {
    ID         string
    TenantID   string
    UserID     string
    Type       MemoryType
    Content    string
    Key        string
    Subtype    string      // 上游 metadata.type（偏好类型等）
    NodeType   string      // 上游 metadata.memory_type
    Tags       []string
    Relativity float64
    Status     string      // activated / deleted
    CreatedAt  UTCTime
    UpdatedAt  UTCTime
}

type SearchQuery struct {
    TenantID     string
    UserID       string
    Text         string
    TopK         int
    Types        []MemoryType
    SessionID    string
    FirstMessage string
    Filter       map[string]any
    DreamTopK    int           // 0 = 不召回 dream
}

type SearchResult struct {
    Facts       []Memory
    Preferences []Memory
    Tools       []Memory
    Dreams      []Memory
    Total       int
}
```

不变量：

- 每个 `Memory` 的 `TenantID` 非空；service 在返回前过滤 `Status == "deleted"` 并按 ID 去重（上游会重复返回同一条）。
- 每类结果按 `Relativity` 降序；`TopK` 对每类分别生效（上游怪癖），由 adapter 展平后在控制面统一截断。
- `Memory` 不直接序列化给调用方：MCP 的 `memory_key`/`memory_value` 等字段在 `transport/mcp` 映射。

### 请求去重

```go
// IdempotencyOutcome 是把请求去重记录收敛到终态的入参；Response 为 nil 表示保留原响应。
type IdempotencyOutcome struct {
    TenantID string
    Key      string
    Response []byte
    At       time.Time
}
```

### 审计、任务与错误

```go
type AuditFilter struct {
    TenantID, Tool string
    Tools          []string // 非空时只取这些工具
    ExcludeTools   []string // 非空时排除这些工具
    Sources        []string // 非空时只取这些入口（AuditSource*）
    OK             *bool    // 非 nil 时按行内 ok 过滤（不合并终态行）
    Status         AuditStatus // 非空时按派生状态过滤（合并同一 call_id 的终态行）
    Text           string   // 不区分大小写的子串匹配（含同 call_id 终态行的 err/output）
    Since, Until   time.Time
    Limit, Offset  int
}

type AuditStatus string // SUCCESS / FAILED / RUNNING

// 调用入口。source 由 transport 写入，service/worker 不推断。
const (
    AuditSourceMCP       = "mcp"
    AuditSourcePlugin    = "plugin" // REST 请求带 X-GraphMemory-Client/User-Agent: graphmemory/*
    AuditSourceREST      = "rest"
    AuditSourceDashboard = "dashboard"
    AuditSourceAdmin     = "admin"
    AuditSourceAuth      = "auth"
)

type AuditHourCount struct {
    Hour time.Time; Tool string; Calls int
    RequestTokens, ResponseTokens, LLMCalls int
}

type AuditRecord struct {
    ID        string
    CallID    string
    TS        time.Time
    Tool      string
    Source    string
    TenantID  string
    UserID    string
    SessionID string
    CaseLike  *bool
    Deferred  bool
    Ms        int64
    OK        bool
    Err       string   // 错误类型名
    ErrMessage string  // 可对外的安全消息
    HTTPStatus int     // 真实 HTTP 状态；后台终态行 200/500
    ResultBytes int
    RequestID string
    RemoteIP  string
    Input     map[string]any
    Output    string
    Query     string
    RequestTokens, ResponseTokens, LLMCalls int // 0 表示上游没有回报
    Status    AuditStatus // 查询时派生，不落库
}

type JobKind string   // 目前只有 memory_add
type JobStatus string // pending / running / succeeded / failed
type Job struct {
    ID        string
    Kind      JobKind
    Status    JobStatus
    TenantID  string
    UserID    string
    Payload   []byte
    Attempts  int
    LastError string
    NextAttemptAt time.Time // 下次可认领时间；零值表示立即
    CreatedAt time.Time
    UpdatedAt time.Time
}

// JobCompletion 是任务终态参数；JobReschedule 是重试调度参数。
type JobCompletion struct {
    ID      string
    Status  JobStatus
    LastErr string
    Now     time.Time
}
type JobReschedule struct {
    ID      string
    Next    time.Time
    LastErr string
    Now     time.Time
}

// JobFilter 是死信排查的查询条件；ListJobs 不返回 Payload。
type JobFilter struct {
    Status   JobStatus
    Kind     JobKind
    TenantID string
    Limit    int
    Offset   int
}
```

错误哨兵与包装：

```go
var (
    ErrNotFound            = errors.New("not found")
    ErrInvalidInput        = errors.New("invalid input")
    ErrUnauthorized        = errors.New("unauthorized")
    ErrForbidden           = errors.New("forbidden")
    ErrConflict            = errors.New("conflict")
    ErrUpstreamUnavailable = errors.New("upstream unavailable")
    ErrUpstreamRejected    = errors.New("upstream rejected")
    ErrUpstreamDenied      = errors.New("upstream denied") // 上游 401/403/404/409，重试不自愈
    ErrDisabled            = errors.New("disabled")
    ErrRateLimited         = errors.New("rate limited")
    ErrTooLarge            = errors.New("too large")    // 内容超过上游或控制面上限（413）
    ErrModelFailed         = errors.New("model failed") // 数据面调用大模型失败（502）
    ErrModelTimeout        = errors.New("model timeout") // 数据面调用大模型超时（504）
)

// VersionConflictError 与 Busy 实现 Is，分别匹配 ErrConflict 与 ErrRateLimited，
// 并额外携带 transport 要放进响应 data 的信息（当前版本号 / 等待时长）。
type VersionConflictError struct{ CurrentVersionID int64 }
type Busy struct {
    Wait    time.Duration
    Message string
}

type Error struct {
    Kind    error          // 上面某个哨兵
    Op      string
    Message string         // 可安全展示给调用方
    Err     error          // 内部原因，不展示
}
func (e *Error) Error() string
func (e *Error) Unwrap() error   // 返回内层 Err；同时保留 Kind 匹配
func Wrap(kind error, op, message string, err error) *Error
func Invalid(op, message string) *Error
func NotFound(op, message string) *Error
func TooLarge(op, message string) *Error
func KindOf(err error) error     // errors.Is 依次匹配哨兵，返回命中的那个
func SafeMessage(err error) string // 领域错误取 Message（不带 Op），其它取 err.Error()

// ReplaySafe 判断按 jobs.last_error 重放死信是否保证发生在上游接受之前。
func ReplaySafe(errName string) bool
```

`Unwrap` 返回内层 `Err`（若有），并用 `Is` 方法同时匹配 `Kind`，保证 `errors.Is(err, ErrUnauthorized)` 成立且 `errors.As` 能取到内部错误。`ReplaySafe` 的白名单是 `not found`/`invalid input`/`unauthorized`/`forbidden`/`conflict`/`upstream rejected`/`upstream denied`/`rate limited`/`disabled`/`too large`；空串与未知类型一律按不安全处理。

### 时间口径

```go
// UTCTime 是真 UTC。上游契约 v2 保证所有响应时间是真实 UTC，
// 控制面不再按记忆类型区分“假 UTC / 真 UTC”。
type UTCTime struct{ time.Time }

func ParseUTC(s string) (UTCTime, error)        // RFC3339，兼容漏偏移的历史写法（按 UTC 解释）
func (t UTCTime) MarshalJSON() ([]byte, error)  // 输出 RFC3339 + Z，零值为 null
func (t UTCTime) String() string                // RFC3339，便于日志

func BeijingClock(raw string) string            // 解析失败原样返回，不让一行脏数据拖垮整页
func BeijingClockOf(t UTCTime) string
func FormatBeijing(t time.Time) string
```

不变量：`domain` 里只有一种上游时间类型。时间口径的差异（历史节点是北京墙钟）由上游在读写边界归一
（见 `docs/module/upstream.md` 的“契约 v2”），控制面不做任何偏移换算，也不按 `NodeType` 分支。
唯一的时区转换是展示层：`BeijingClock*` 把 UTC 换成北京时间。

## 错误与边界情况

- `KindOf` 对未包装的普通 error 返回 `nil`，由调用方决定兜底（transport 映射为 500）。
- `Error.Message` 不得包含上游地址、token、记忆正文；内部原因放在 `Err`。
- `ParseUTC` 对空串返回零值且不报错（上游字段可能缺失），格式非法返回错误。

## 测试与验证

- token：随机性与格式、哈希稳定、过期边界、掩码、`Validate` 的 admin/tenant 约束。
- 能力矩阵：每个角色对 5 个能力的表驱动断言；非法角色恒不满足；已认证主体（含管理 Token）都有 `CapProjectCreate`；平台管理员对任意项目与平台能力为 true。
- 主体角色：`RoleIn` 对平台管理员返回 owner，`MembershipIn` 只返回真实成员关系。
- 邀请：`NewInviteToken` 的前缀与长度、随机性；`Status` 的四种状态与优先级（revoked > used > expired > active）、`expires_at == now` 视为过期；`HashInviteToken` 稳定且不等于明文。
- 个人项目与归档：`PersonalTenantID` 确定性与 `u-` 前缀；`Tenant.Archived` 对零值 `ArchivedAt` 返回 false。
- 错误：`errors.Is`/`errors.As` 穿透包装；`KindOf`；`ReplaySafe` 的白名单与未知类型。
- 时间：`ParseUTC` 把 `+08:00` 换算成 UTC（`20:34:56+08:00` → `12:34:56Z`），漏偏移的写法按 UTC 解释；`BeijingClock` 输出北京时间，解析失败原样返回。
- 本地单元测试。

## 参考来源

- `docs/ARCHITECTURE.md` 的“租户与身份”“上游约束”“时间口径”。

## 实现记录

See the public version-control history.
