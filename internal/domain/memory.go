package domain

import "encoding/json"

// MemoryType 是记忆桶。上游按桶分别返回，top_k 也按桶生效。
type MemoryType string

const (
	MemoryTypeText  MemoryType = "text"
	MemoryTypePref  MemoryType = "pref"
	MemoryTypeTool  MemoryType = "tool"
	MemoryTypeDream MemoryType = "dream"
)

// Memory 是控制面视角的一条记忆。
type Memory struct {
	ID         string
	TenantID   string
	UserID     string
	Type       MemoryType
	Content    string
	Key        string
	Subtype    string // 上游 metadata.type，偏好类型等
	NodeType   string // 上游 metadata.memory_type：LongTermMemory、UserMemory、PreferenceMemory 等
	Tags       []string
	Relativity float64
	Status     string // activated / deleted
	CreatedAt  UTCTime
	UpdatedAt  UTCTime
	Version    int
	History    []MemoryVersion
	Sources    []MemorySource
}

// Deleted 表示上游的软删除记录。上游向量清理滞后，检索仍会返回它们。
func (m Memory) Deleted() bool { return m.Status == "deleted" }

// ContractTopKMax 是上游契约 v2 公布的 top_k 上限（契约字段 top_k_max）。
// MEMOSD_MAX_TOP_K 的校验与启动自检都引用它，避免两处各写一个数然后漂移。
const ContractTopKMax = 25

// SearchQuery 是一次检索的输入。TopK 对每个记忆桶分别生效，由控制面统一截断。
type SearchQuery struct {
	TenantID     string
	UserID       string
	Text         string
	TopK         int
	Types        []MemoryType
	SessionID    string
	FirstMessage string
	Filter       map[string]any
	DreamTopK    int // 0 表示不召回 dream 桶
	// Mode 透传给上游检索策略：fast 只走向量，fine 走改写与补召回。
	// 空表示用上游默认值。
	Mode string
}

// SearchResult 是展平、去重、排序后的检索结果。
type SearchResult struct {
	Facts       []Memory
	Preferences []Memory
	Tools       []Memory
	Dreams      []Memory
	Total       int
}

// ToolCallFunction 是工具调用的函数名与参数。
type ToolCallFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"` // JSON 字符串，上游按字符串收
}

// ToolCall 是 assistant 消息里的工具调用轨迹。
type ToolCall struct {
	ID       string           `json:"id"`
	Type     string           `json:"type"`
	Function ToolCallFunction `json:"function"`
}

// Message 是归一后的对话消息，字段名对齐上游 ChatCompletion 入参。
type Message struct {
	Role       string     `json:"role"`
	Content    any        `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
	Name       string     `json:"name,omitempty"`
}

// AddPayload 是上游写入所需的归一结果。
type AddPayload struct {
	TenantID  string
	UserID    string
	SessionID string
	Messages  []Message
	Tags      []string
}

// AddRequest 是一次写入。Messages 保留原始 JSON（可能是对象数组，也可能是
// 被客户端二次编码的字符串），归一在 memory 服务里做。
type AddRequest struct {
	TenantID       string
	UserID         string
	SessionID      string
	FirstMessage   string
	Messages       json.RawMessage
	Tags           []string
	Async          bool
	IdempotencyKey string // 非空时同一租户下重复请求只执行一次
	// CallID 是调用方审计行的 call_id；异步入队时用作 job.ID，使后台终态行与
	// 请求行同 call_id。同步路径不使用。
	CallID string
}

// AddResult 是写入结果。Usage 来自上游旁路字段，老上游可能为空。
type AddResult struct {
	Status   string
	Message  string
	Usage    map[string]int
	Deferred bool // true 表示只入队，抽取由 worker 继续
	// CallID 是异步路径实际使用的 job ID。它随幂等缓存保存，重放时带回首次请求的
	// call_id，transport 据此让重放请求行与首次任务的终态行对上；同步路径为空。
	CallID string `json:"call_id,omitempty"`
	// MemoryIDs 只保留上游确认写入的ID，用于完成后的活动详情，不改变工具受理响应。
	MemoryIDs []string `json:"memory_ids,omitempty"`
	// Snapshot 仅交给worker持久化，不向工具调用方回传或进入幂等响应缓存。
	Snapshot *MemoryWriteSnapshot `json:"-"`
}

// MemoryActivity 是一次写入完成后的真实记忆结果，不包含原始审计入参。
type MemoryActivity struct {
	CallID      string
	Status      string
	CompletedAt string
	Memories    []Memory
	Total       int
	Truncated   bool
}

// DeleteResult 是删除结果。NotFoundIDs 是确认不存在、因此没有删除的 id；
// 任一 id 属于其他空间时整批拒绝，不会走到这里。
type DeleteResult struct {
	DeletedIDs  []string
	NotFoundIDs []string
}

// MemoryUpdate 仅开放正文与标题，不允许借元数据改归属或版本号。
type MemoryUpdate struct {
	ID, TenantID, UserID, Content string
	Title                         *string
	ResetContentTags              bool
}

type MemoryUpdateResult struct {
	Memory  Memory
	Changed bool
}

// FeedbackChange 是上游反馈记录里的一条新增或更新。
type FeedbackChange struct {
	ID         string
	Text       string
	ArchivedID string
}

// FeedbackRequest 是一次纠错反馈。
type FeedbackRequest struct {
	TenantID           string
	UserID             string
	SessionID          string
	FirstMessage       string
	Content            string
	RetrievedMemoryIDs []string
}

// FeedbackResult 是反馈结果，Added/Updated 是上游改动的唯一证据。
type FeedbackResult struct {
	Status    string
	Message   string
	Added     []FeedbackChange
	Updated   []FeedbackChange
	Truncated bool
}

// ProfileQuery 是画像查询。
type ProfileQuery struct {
	TenantID          string
	UserID            string
	IncludePreference bool
	IncludeToolMemory bool
	Page              int
	PageSize          int
}

// ProfileResult 是画像结果。
type ProfileResult struct {
	Facts       []Memory
	Preferences []Memory
	Tools       []Memory
	Total       int
	Current     int
	Size        int
}

// DashboardBucket 是 get_memory_dashboard 返回的桶。
type DashboardBucket string

const (
	DashboardBucketText DashboardBucket = "text_mem"
	DashboardBucketPref DashboardBucket = "pref_mem"
	DashboardBucketTool DashboardBucket = "tool_mem"
)

// DashboardQuery 是控制台记忆列表的一次分页读取。Filter 原样透传给上游。
type DashboardQuery struct {
	TenantID string
	Bucket   DashboardBucket
	Page     int
	PageSize int
	Filter   map[string]any
}

// DashboardRow 是列表里一条记忆的白名单投影；不含 sources、embedding 等大字段。
// 时间是上游契约 v2 承诺的真 UTC，展示时再换成北京时间。
type DashboardRow struct {
	ID         string
	Content    string
	NodeType   string
	CreatedAt  string
	UserID     string
	TemplateID string
	InstanceID string
}

// DashboardPage 是一页结果与筛选后的总数。
type DashboardPage struct {
	Rows  []DashboardRow
	Total int
}

// MemoryVersion 保留上游的历史快照，不从当前内容推断旧版本。
type MemoryVersion struct {
	Version    int    `json:"version"`
	Content    string `json:"content"`
	CreatedAt  string `json:"createdAt"`
	UpdateType string `json:"updateType,omitempty"`
	ValidFrom  string `json:"validFrom,omitempty"`
	ValidTo    string `json:"validTo,omitempty"`
}

// MemorySource 仅投影详情展示需要的来源，不带向量和任意内部元数据。
type MemorySource struct {
	Type    string `json:"type,omitempty"`
	Role    string `json:"role,omitempty"`
	Content string `json:"content,omitempty"`
	Time    string `json:"time,omitempty"`
	URL     string `json:"url,omitempty"`
}
