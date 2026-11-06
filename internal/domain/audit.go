package domain

import "time"

// AuditStatus 是控制台调用日志的状态。`SUCCESS`/`FAILED` 可以是请求行自身的
// 结果，也可以来自同一 call_id 的后台终态行；`RUNNING` 表示异步 add 还没有终态行，
// 且对应 job 仍在 jobs 保留期内（job 清理后按行内 ok 回退）。
type AuditStatus string

const (
	AuditStatusSuccess AuditStatus = "SUCCESS"
	AuditStatusFailed  AuditStatus = "FAILED"
	AuditStatusRunning AuditStatus = "RUNNING"
)

// AuditFilter 是审计查询条件。类型放在 domain，避免 adapter 为声明端口而 import service。
type AuditFilter struct {
	TenantID string
	CallID   string
	Tool     string
	Tools    []string // 非空时只取这些工具
	// ExcludeTools 非空时排除这些工具。控制台操作日志用它与网关工具互补，
	// 不需要把所有工具名重复列一遍。
	ExcludeTools []string
	// Sources 非空时只取这些入口（AuditSource*）。空字符串不匹配任何值，
	// 历史行的 source 为空，按来源筛选中看不到它们——这是刻意的：不清楚来源
	// 的行不该被算进某一条入口的账里。
	Sources []string
	OK      *bool       // 非 nil 时按行内 ok 过滤（不合并后台终态行）
	Status  AuditStatus // 非空时按派生状态过滤（合并同一 call_id 的后台终态行）
	Text    string      // 非空时在入参、输出、查询词、错误、会话与租户里做不区分大小写的子串匹配
	Since   time.Time   // 含
	Until   time.Time   // 不含
	Limit   int
	Offset  int
}

// AuditSource 是审计记录的调用入口。
//
// 工具名回答“调了什么”，来源回答“从哪进来的”，两者不能互相推导：同一个
// add_message 既可能来自 MCP 工具调用，也可能来自记忆插件走的 /memory/add 门面，
// 运维含义完全不同。
//
// 插件与直连脚本走的是同一批 REST 端点，靠客户端自报的标识区分（见
// transport/rest 的 requestSource）：插件报 plugin，没有标识的算 rest。
//
// 由各入口 transport 写入，service/worker 层不推断——那一层看不到请求来源，
// 猜只会把“谁在压上游”猜错。异步 add 的终态行不重复写来源：它与请求行同
// call_id，列表合并、详情补用量，来源以请求行为准。
const (
	AuditSourceMCP       = "mcp"
	AuditSourcePlugin    = "plugin"
	AuditSourceREST      = "rest"
	AuditSourceDashboard = "dashboard"
	AuditSourceAdmin     = "admin"
	AuditSourceAuth      = "auth"
)

// AuditHourCount 是某租户在一个 UTC 整点内某工具的调用次数与 token 用量。
// 北京时间与 UTC 相差整数小时，所以整点桶可以无损换算成北京日期。
type AuditHourCount struct {
	Hour           time.Time
	Tool           string
	Calls          int
	RequestTokens  int
	ResponseTokens int
	LLMCalls       int
}

// AuditRecord 是一次工具调用或 HTTP 请求的审计记录。
// 字段口径对齐既有网关的 calls-*.jsonl，Dashboard 直接按这些字段读取。
type AuditRecord struct {
	ID     string
	CallID string
	TS     time.Time
	Tool   string
	// Source 是调用入口（AuditSource*）。历史行与异步 add 的终态行为空。
	Source    string
	TenantID  string
	UserID    string
	SessionID string
	CaseLike  *bool
	Deferred  bool
	Ms        int64
	OK        bool
	Err       string
	// ErrMessage 是错误的安全消息（不含上游地址、SQL 细节），供控制台详情展示。
	// Err 仍是错误类型名，两者分开保存，过滤与聚合不受文案变化影响。
	ErrMessage string
	// HTTPStatus 是这次调用的真实 HTTP 状态；后台任务行写 200/500，
	// 历史行与未填写的路径为 0，读取方回退到按 OK 推导。
	HTTPStatus  int
	ResultBytes int
	// RequestID 是 X-Request-Id，用于把审计行与同一请求的日志对上；后台任务为空。
	RequestID string
	// RemoteIP 是 TCP 对端 IP（不信任 X-Forwarded-For），后台任务为空。
	RemoteIP string
	// ClientVersion / HostVersion 是插件与宿主自报的版本（graphmemory/0.2.12、
	// hermes/0.21.5）。MCP 入口与历史行为空。
	ClientVersion string
	HostVersion   string
	// AgentIdentity 是部署身份（同一套 hermes 的多份部署），AgentContext 是
	// 宿主给的上下文（primary / subagent / cron / flush）。
	AgentIdentity string
	AgentContext  string
	// TurnNumber 是插件自报的轮次序号；0 表示没报或不是插件入口。
	TurnNumber int
	Input      map[string]any
	Output     string
	Query      string
	// 模型 token 用量来自上游 add 响应的 usage 旁路字段；0 表示没有可观测数据。
	// 与旧网关 add-bg-*.jsonl 的 request_tokens/response_tokens/llm_calls 同口径。
	RequestTokens  int
	ResponseTokens int
	LLMCalls       int
	// Status 是查询时按 call_id 合并后台终态行得到的派生状态，不落库；
	// 写入方向不需要填，列表与详情由存储实现填充。
	Status AuditStatus
}

// JobKind 是异步任务的类型。
type JobKind string

const (
	JobKindMemoryAdd JobKind = "memory_add"
	// JobKindLocalPromote 把本地记忆镜像转存为云端记忆，由 worker 调上游 add_message。
	JobKindLocalPromote JobKind = "local_promote"
)

// JobStatus 是异步任务的状态。
type JobStatus string

const (
	JobStatusPending   JobStatus = "pending"
	JobStatusRunning   JobStatus = "running"
	JobStatusSucceeded JobStatus = "succeeded"
	JobStatusFailed    JobStatus = "failed"
)

// Valid 判断状态是否是已知取值，供查询参数校验使用。
func (s JobStatus) Valid() bool {
	switch s {
	case JobStatusPending, JobStatusRunning, JobStatusSucceeded, JobStatusFailed:
		return true
	default:
		return false
	}
}

// Job 是落库的异步任务。Payload 是入队时的请求快照（JSON），
// 重试语义由 worker 决定，控制面不做跨进程去重。
type Job struct {
	ID        string
	Kind      JobKind
	Status    JobStatus
	TenantID  string
	UserID    string
	Payload   []byte
	Attempts  int
	LastError string
	// NextAttemptAt 是下次可认领时间；零值表示立即可认领。重试复用同一行，
	// 行、job ID、created_at 与 attempts 都保持不变。
	NextAttemptAt time.Time
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

// JobCompletion 是任务终态参数。
type JobCompletion struct {
	ID      string
	Status  JobStatus
	LastErr string
	Now     time.Time
}

// JobReschedule 是重试调度参数。
type JobReschedule struct {
	ID      string
	Next    time.Time
	LastErr string
	Now     time.Time
}

// JobFilter 是异步任务的查询条件，供死信排查使用。
// ListJobs 不返回 Payload：记忆正文不进入运维响应。
type JobFilter struct {
	Status   JobStatus
	Kind     JobKind
	TenantID string
	Limit    int
	Offset   int
}
