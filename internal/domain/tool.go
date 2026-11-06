package domain

// 对外暴露的工具名（MCP 与 REST 共用）是跨进程契约：工具描述、golden、审计统计与
// 控制台的能力声明都以它们为准，改名必须同时改 golden 与前端。
const (
	ToolAddMessage     = "add_message"
	ToolSearchMemory   = "search_memory"
	ToolDeleteMemory   = "delete_memory"
	ToolUpdateMemory   = "update_memory"
	ToolAddFeedback    = "add_feedback"
	ToolGetUserProfile = "get_user_profile"
)

// ToolAddMessageBackground 是异步 add 的后台终态行工具名。它不属于对外工具，
// 只用来把 worker 的成败与请求行按 call_id 对上。
const ToolAddMessageBackground = ToolAddMessage + ".background"

// GatewayTools 返回控制台“调用日志/用量”与 /api/meta 声明的 6 个对外工具，顺序即展示顺序。
// 每次返回新切片，调用方改写不会影响其他包。
func GatewayTools() []string {
	return []string{ToolAddMessage, ToolSearchMemory, ToolUpdateMemory, ToolDeleteMemory, ToolAddFeedback, ToolGetUserProfile}
}
