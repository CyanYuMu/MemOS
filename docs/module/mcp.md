---
module: mcp
packages:
  - internal/transport/mcp
  - internal/transport/response
status: implemented
---

# mcp

MCP入站协议：`/mcp` Streamable HTTP，六个记忆工具。名称、参数和描述由控制面契约维护并以golden锁定；新增修改工具、严格的消息对象数组、明确意图下的写入和删除流程见下表。

## 职责与边界

- 负责：HTTP 上的 MCP 协议（mcp-go v1.3.0）、从 `Authorization` 取 token 并调用 identity、工具参数到 service 入参的转换、service 结果到响应体字段的映射（`response`）、工具级审计。
- 不负责：业务规则（memory）、Token 校验（identity）、上游调用（upstream）。工具参数里没有 `user_id`；身份只来自 HTTP 头。

## 依赖

- `internal/transport/mcp` 依赖 `internal/service/memory`、`internal/service/identity`、`internal/service/audit`、`internal/transport/httpx`、`github.com/mark3labs/mcp-go`（精确 `v1.3.0`）。
- `internal/transport/response` 只依赖 `internal/domain`，供 mcp 与 rest 共用。

## 契约

```go
type Deps struct {
    Memory   *memory.Service
    Identity *identity.Service
    Audit    *audit.Recorder
    Logger   *slog.Logger
    Name     string
    Version  string
    Limits   memory.Options // 只读 top_k 上限
}

func New(deps Deps) (*Server, error)
func (s *Server) Handler() http.Handler // 挂到 "POST /mcp"（以及 GET/DELETE 由 mcp-go 处理）
```

- 请求去重：MCP 协议不定义幂等/重试语义，也不该要求模型或客户端生成幂等键。`add_message` 不带任何键，由 service 在消息归一后按内容哈希与写入模式自动去重：同一请求重发返回首次结果，不重复入队、不重复写上游；内容不同则是新请求，照常写入。TTL 24h，见 `memory` 模块。
- 异步：`add_message` 固定 `Async=true`，入队后立即返回 `{status:"success",message:"Memory accepted..."}`，不阻塞 agent——与 dumemos 的 async 语义一致（请求立即返回、抽取在后台完成）。本服务用自己的 `jobs` 队列承接，worker 在后台**同步**调用上游，以便拿到真实成败并重试；`AddAsync` 关闭或入队失败时才退回请求内同步。
- 错误：service 的错误转成工具级错误。`invalid_input`/`unauthorized`/`forbidden`/`not_found`/`conflict`/`upstream_rejected`/`disabled` 原样返回消息；其余内部错误统一返回“内部错误，请稍后重试”，不暴露上游地址或存储细节。不允许 panic 穿透（mcp-go `WithRecovery`）。
- 写入结果的归属：`add_message` 的 2xx 只表示「已入队」，最终一致性由 [worker](worker.md) 保证 —— 重试、`add_message.background` 终态审计行、死信可见、`POST /admin/jobs/{id}/retry` 人工重放。客户端**不需要**轮询结果，也不该给模型加「查询写入状态」的工具：失败留在服务端、人可见可处理。这条同样适用于走 REST `/memory/add` 的客户端（例如 hermes 记忆插件），两条入口的可靠性语义一致。
- 输出：`mcp.NewToolResultText(<JSON>)`，JSON 由 `response` 生成；`add_message` 入队时返回 `{status,message,deferred:true,call_id}`，同步回退（`AddAsync` 关闭或入队失败）时只有 `{status,message}`。`call_id` 与任务 id 相同，排障时可在任务表/死信页按它定位。

### 6 个工具

| 工具 | 入参 | 返回 |
|------|------|------|
| `add_message` | `conversation_first_message`(string, required)、`messages`(array<{role,content}>, required)、`tags`(array<string>, optional)、`session_id`(string, optional) | `{status,message[,deferred,call_id]}`；入队时带 `deferred:true` 与任务 id |
| `search_memory` | `query`(string, required)、`filter`(object, optional)、`top_k`(integer, default 10)、`conversation_first_message`(string, optional)、`session_id`(string, optional) | `{memory_detail_list,preference_detail_list,tool_memory_detail_list[,dream_memory_detail_list]}` |
| `update_memory` | `memory_id`、`content`必填，`title`可选 | 返回已读回确认的记忆、实际version与changed |
| `delete_memory` | `memory_ids`(array<string>, required) | `{status,deleted_ids[,not_found_ids],message}`；任一 id 属于其他空间时整批拒绝 |
| `add_feedback` | `feedback_content`(string, required)、`conversation_first_message`(string, default "")、`retrieved_memory_ids`(array<string>, optional)、`session_id`(string, optional) | `{status,message,added,updated[,truncated]}` |
| `get_user_profile` | `include_preference`(bool, default true)、`include_tool_memory`(bool, default false)、`current`(integer, default 1)、`size`(integer, default 20) | `{memory_detail_list,preference_detail_list,tool_memory_detail_list,current,size,total}` |

schema通过`mcp.NewToolWithRawSchema`提供。当前控制面维护工具说明，不再从旧网关生成。`add_message`只在明确要求记录时调用；删除后不自动提取删除意图；具体纠正优先`update_memory`，自然语言反馈仍可用`add_feedback`。`search_memory`的dream变体只增加摘要桶说明。

### `session_id` 入参（2026-10-23）

- 背景：此前三个会话相关工具只有 `conversation_first_message`，调用方即使有稳定的会话 id 也传不进来，service 只能退化为 `sha1(首条消息)` 的内容寻址；不同会话开场白相同就会坍缩成同一个 id（影响面见 `memory.md` 的“会话标识”）。
- 实现：`add_message`、`search_memory`、`add_feedback` 各增加**可选** `session_id`（string，默认 `""`）。显式值优先，缺失时仍回落首条消息哈希，所以既有客户端行为不变；`required` 不变。工具 handler 把它透传给 service，审计的 `SessionID` 同口径用 `ResolveSessionID(explicit, first_message)` 复算。
- 不做的：不在服务端为无 id 的请求生成随机会话 id（每轮都变，等于放弃同会话加权），也不用时间桶哈希（同一天同开场白仍碰撞，且切断跨午夜会话）。

### golden

- 测试把 `MCPServer.ListTools()` 序列化后与 golden 比较；改 golden 必须在提交说明里写原因。

### 响应字段形状（`response`）

```go
func Search(result domain.SearchResult, includeDream bool) map[string]any
func Profile(result domain.ProfileResult) map[string]any
func Add(result domain.AddResult) map[string]any
func Delete(result domain.DeleteResult) map[string]any
func Feedback(result domain.FeedbackResult) map[string]any
func Encode(v any) string // JSON，去掉 nil 字段，缩进 1 个空格
```

- fact：`memory_key`/`memory_value`/`relativity`/`create_time`/`tags`/`memory_id`；
- preference：`preference`/`preference_type`/`relativity`/`create_time`/`memory_id`；
- tool：`tool_value`/`relativity`/`create_time`/`memory_id`；
- dream：与 fact 同形，`memory_key` 取 title。
- 空列表输出 `[]`（不是 null）；时间用 `domain.UTCTime` 序列化；`create_time` 为零值时省略。
- 这里的字段形状就是本服务的对外契约，MCP 工具与 REST 共用；与 memos 网关兼容只是来源，改字段要先改本节与 golden。

### 审计

每个工具调用写一条 `domain.AuditRecord`：`tool`、`Source=mcp`、`TenantID`/`UserID`、`SessionID`、`CaseLike`（从 messages 做正则判断，只看不拦）、`Ms`、`OK`、`Err`（错误类型名）、`ResultBytes`、`Input`（参数原样，不脱敏；JSONL 归档保存完整原文，SQLite 查询副本超过单条 256 KiB 时按字段预算截断并留标记）、`Output`（结果原样）、`Query`（search 的 query）、`CallID`。审计失败不影响工具结果。未认证的调用不写工具审计；它的原始请求仍由 `Handler` 写 `mcp.request` 归档。

`Handler` 在鉴权**之前**把每个 `POST /mcp` 的原始 body 写一条 `Tool=mcp.request` 的归档（`Input.body` + method/path/content-type/user-agent/body_bytes；鉴权失败 `ok=false,http_status=401`，body 超限 `truncated=true,http_status=413`）。这条记录只进 JSONL，不进 SQLite；`GET`/`DELETE` 没有 body，不写。

`initialize`、`tools/list`、`ping`、`notifications/*` 等非 `tools/call` 的 JSON-RPC 方法在认证后另写**归档文件**（`Tool=mcp.<method>`，`Input` 含 method 与 params），但不进 SQLite、不出现在控制台调用日志；`tools/call` 由 `invoke` 写完整工具审计，不重复记协议行。这样“所有 MCP 操作”都有落盘记录，业务查询仍只保留 5 个工具。

## 错误与边界情况

- `top_k`/`size` 超限由 service 夹取，schema 不写 maximum（与网关一致）。
- 工具名/描述/schema 改动 = 契约变更，必须先改本文与 golden。
- 鉴权失败发生在协议层之前：`initialize`/`tools/list` 返回 HTTP 401，不会出现工具级鉴权错误；直接调用工具 handler 的单元测试仍保留兜底解析。

## 测试与验证

- golden：`ListTools` 与 `test/golden/mcp/tools.json` 全等。
- HTTP 层：无 token 的 `initialize` 与 `tools/list` 都返回 401（且不泄漏工具清单）；带 token、不带会话的 `tools/list` 返回 200；`initialize` 不下发 `Mcp-Session-Id`。
- handler：用 `mcptest` 或直接调用 handler 函数，覆盖 add/search/delete/feedback/profile 的成功与错误路径、审计字段。
- 第 2 层验证：本地进程 + `forward-upstream`，5 个工具真实冒烟（G6）。
- 与 memos MCP 文档逐项对照的差异写进实现记录。

## 参考来源

- `docs/ARCHITECTURE.md` 的“入站协议”“租户与身份”。

## 实现记录

See the public version-control history.
## 个人助手能力补齐（2026-10-31，accepted）

- 新增 `update_memory` / `POST /memory/update`，只接受memory_id、content与可选title；身份与作用域来自凭据。控制面先查归属，调用已有 `/product/update_memory` 后读回验证正文、标题与版本，不把受理当更新成功。重复相同正文不递增版本，禁止删除再新增模拟修改。
- 更新复用底座的嵌入重算和history，返回真实记忆投影供聊天日志展开。当前项目外、空正文、已删除或不存在记录必须拒绝。
- 个人助手默认开放六个记忆工具，具体删改仅按当前用户明确意图；检索内容不构成授权。环境仍可覆盖工具白名单。纠正反馈需检查目标ID归属，成功回执不代表产生了修改。
- 回答评分仍独立持久化；用户可在回答旁明确输入纠正，作为用户消息进入已有聊天链路，绝不由点赞/点踩推断事实。
- 验证：服务归属/no-op/上游失败、适配器请求作用域与错误、MCP/REST契约、配置工具暴露，以及dev任务项目真实写查改版本删闭环。



记忆事件关联（2026-10-31）：成功响应附加call_id，对异步Add沿用首次jobID，其他工具采用入口生成的调用ID。将相同可信ID经memory.WithEventScope传入用例，形成真实结果/事件/工具历史的关联；该字段不用于鉴权。现有字段与错误格式保持，事件契约见memory-events模块。

失败调用关联（2026-10-31，implemented）：已认证工具失败保留isError及首个可读错误文本，并附加JSON文本块{status:"failed",call_id}，关联同次失败事件和审计。未认证错误不返回关联标识，不改变HTTP鉴权、工具schema或模型权限。

失败关联验证：MCP race/shuffle、fmt-check/lint-new/vet/docs-check通过，两位独立只读复审通过，确认Pi保留多文本块；前端43tests/verify通过，含失败日志去重和changed=false计数0。
