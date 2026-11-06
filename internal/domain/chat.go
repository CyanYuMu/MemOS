package domain

import (
	"context"
	"encoding/json"
	"errors"
	"time"
)

// Chat 模块的领域错误。transport 按这三个哨兵映射状态码：
// 缺前置条件 409、进程数超限 429、运行时不可用 503。
var (
	ErrChatNotReady    = errors.New("chat not ready")
	ErrChatBusy        = errors.New("chat busy")
	ErrChatUnavailable = errors.New("chat unavailable")
)

// ChatConfig 是一个项目（租户）的 BYOK 模型配置。APIKey 是明文，只用于注入
// pi 的进程环境与生成 models.json，不经任何响应返回。
type ChatConfig struct {
	TenantID  string
	BaseURL   string
	Model     string
	APIKey    string
	UpdatedAt time.Time
}

// ChatSessionSpec 是启动一次对话进程所需的全部信息。运行时实现只依赖它，
// 不接触 service 里的项目、Key 与配置查询。
type ChatSessionSpec struct {
	SessionID      string
	TenantID       string
	AccountID      string
	ConversationID string
	// ProjectKey 是所选项目 API Key 的明文，作为 MEMOS_MCP_TOKEN 注入 pi。
	ProjectKey string
	BaseURL    string
	Model      string
	// APIKey 是用户自配模型网关的明文，作为 PI_BYOK_KEY 注入。
	APIKey string
}

// ChatConversation 只存对话入口，正文仍由运行时的独立历史保存。
type ChatConversation struct {
	ID        string    `json:"id"`
	TenantID  string    `json:"-"`
	AccountID string    `json:"-"`
	Title     string    `json:"title"`
	CreatedAt time.Time `json:"createdAt"`
}

// ChatSubmission 是一条已被受理的消息。ID 由服务端生成，前端用它把
// message_accepted 事件与本地时间线上的用户消息对上。
type ChatSubmission struct {
	ID string `json:"id"`
	// Queued 表示受理时已有在途轮次，这条要等当前轮结束后才会发送。
	Queued bool `json:"queued"`
}

// ChatEvent 是会话推给订阅者的一条事件。Seq 由会话对象单调分配，进程重启不重置；
// 前端按 Type 渲染，Seq 只用于去重与调试。
type ChatEvent struct {
	Seq          int64
	Type         string
	Data         json.RawMessage
	SubmissionID string
}

// ChatSession 是一个（账号，项目）的会话对象。
//
// 会话对象常驻，pi 进程可有可无：Subscribe / Messages 不会启动进程，只有 Submit
// 会。事件广播由实现负责：订阅者 channel 满时丢弃该订阅者，绝不阻塞 pi 的 stdout。
type ChatSession interface {
	// Submit 受理一条用户消息并立即返回。消息先落盘再入队，保证被受理的消息
	// 不会静默丢失。
	Submit(ctx context.Context, text string) (ChatSubmission, error)
	// Retry 把最早一条待重试消息重新入队。返回它新的受理信息；没有待重试
	// 消息时返回 ErrNotFound。
	Retry(ctx context.Context) (ChatSubmission, error)
	// Abort 中断当前轮并清空排队；进程不存在时直接成功。
	Abort(ctx context.Context) error
	// Messages 返回历史消息。进程在时读实时结果，不在时读上一轮落下的快照。
	Messages(ctx context.Context) (json.RawMessage, error)
	// Subscribe 注册事件订阅者，返回取消函数；取消只注销订阅，不停进程。
	Subscribe(ctx context.Context) (<-chan ChatEvent, func())
}

// ChatAnswerFeedback 是独立于记忆纠正的回答评价记录。
type ChatAnswerFeedback struct {
	Vote           int    `json:"vote"`
	Comment        string `json:"comment,omitempty"`
	ConversationID string `json:"conversationId,omitempty"`
	AnswerExcerpt  string `json:"answerExcerpt,omitempty"`
	UpdatedAt      string `json:"updatedAt,omitempty"`
}
