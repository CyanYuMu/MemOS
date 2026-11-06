---
module: rest
packages:
  - internal/transport/rest
status: implemented
---

# rest

给 Hermes 记忆插件用的 REST 入口：`POST /memory/{add,search,delete,feedback,profile}`。与 MCP 工具同源同语义，区别只是入参来自 JSON body。`add` 与 MCP 一样**入队即返回**，不让调用方等在抽取上。

## 职责与边界

- 负责：HTTP 编解码、Bearer 鉴权、宽松参数解析（字符串布尔/数字）、service 调用、错误状态码、审计。
- 不负责：业务规则、上游调用、MCP 协议。

## 依赖

- `internal/service/memory`、`internal/service/identity`、`internal/service/audit`、`internal/transport/httpx`、`internal/transport/response`。
- 与 `transport/mcp` 共用 service 与输出形状。

## 契约

```go
type Deps struct {
    Memory   *memory.Service
    Identity *identity.Service
    Audit    *audit.Recorder
    Logger   *slog.Logger
}

func New(deps Deps) *Server
func (s *Server) Handler() http.Handler // 挂到 /memory/
```

### 路由

| 路径 | body 关键字段 | 行为 |
|------|---------------|------|
| `POST /memory/add` | `messages`（数组或 JSON 字符串）、`conversation_first_message`、`tags`、`session_id`；可选请求头 `Idempotency-Key`（或 body `idempotency_key`） | **异步**写入（`Async=true`）：入队即返回 `{status,message,deferred:true,call_id}`，抽取由 worker 继续；失败进任务表/死信页。默认按归一内容自动去重，显式键可覆盖，同键不同内容 409 |
| `POST /memory/search` | `query`、`filter`、`top_k`、`conversation_first_message`、`session_id`、`mode`（`fast`/`fine`/`mixture`，缺省用上游默认） | 返回检索结构 |
| `POST /memory/update` | memory_id、content、可选title | 归属检查后更新并读回确认，返回实际version；同文no-op不加版本 |
| `POST /memory/delete` | `memory_ids`（数组或字符串） | 返回 `{status,deleted_ids[,not_found_ids],message}`；任一 id 属于其他空间时整批拒绝（403） |
| `POST /memory/feedback` | `feedback_content`、`conversation_first_message`、`retrieved_memory_ids`、`session_id` | 返回反馈摘要 |
| `POST /memory/profile` | `include_preference`、`include_tool_memory`、`current`、`size` | 返回画像 |
| `GET /memory/whoami` | 无 | 回显 `user_id`、`project_id`、`project_name`，安装脚本用它确认 token 落在哪个项目；不写审计 |

`/memory/whoami` 是唯一不符合“与 MCP 工具一一对应”的路径：它不操作记忆，只做 token 自检。放在 `/memory/` 下是因为零信任只放行了 `/mcp` 与 `/memory/*` 两个前缀。

- 鉴权：`Authorization: Bearer <token>`；缺失或无效 401 `{"status":"error","message":"authenticate: 无法确定调用方身份：请使用分配给自己的 Bearer token 访问本服务"}`。
- 成功：HTTP 200，body 是 `response` 包生成的 JSON 对象（与 MCP 工具返回的文本内容相同）。
- 失败：`{"status":"error","message":"..."}`；400 参数错误、401 身份、403 禁止、409 请求去重冲突、413 请求体过大、415 非 JSON、502 上游拒绝、503 上游不可用、500 其他。
- 请求去重：与 MCP 共用 service 的自动去重，`Idempotency-Key` 不是必填；传了就以它为准（同键不同内容 409），不传则按 `(tenant_id, 归一 payload 哈希, 异步开关)` 去重。MCP 与 REST 现在同为异步，同一段对话从任一入口进来都只写一次。
- `Content-Type` 必须是 `application/json`（有 body 时），否则 415。
- 宽松解析：字符串 `"true"/"1"/"yes"/"on"` 视为 true；数字字符串转 int；`memory_ids` 传字符串时视为单元素数组；非法 JSON 400。

### OpenAPI

- 规范文件：`api/openapi.yaml`（OpenAPI 3.0.3），覆盖 5 条 `POST /memory/*`、错误响应与 `bearerAuth`。
- `internal/transport/rest/openapi_test.go` 用 `getkin/kin-openapi` 加载规范，校验真实请求和响应；路由或响应形状改动会让测试失败。规范只做契约校验，不参与运行时编解码，也不生成 server stub。

### 审计

与 MCP 同名字段：`tool` 用 `add_message` 等工具名（Dashboard 按工具名聚合），`source` 默认 `rest`；带 `X-GraphMemory-Client: graphmemory/<version>`（或同值的 `User-Agent`）的请求记为 `plugin`，用来把插件流量与直连脚本分开。`Input` 保存原始 body（不脱敏；超过单条 256 KiB 总量上限时按字段预算截断并留标记），`SessionID` 由 service 折算后的值经 `memory.ResolveSessionID` 复算，失败记录 `err` 类型名。异步 `add` 的请求行 `deferred=true`，终态行由 worker 另写。鉴权在解析 body 之前完成；未认证请求（缺 token/无效 token）不写审计，避免无凭据流量带着 4 MB body 进队列。

## 错误与边界情况

- 请求体上限由 `httpx.MaxBody` 统一限制（4MiB）。
- 未知字段忽略；`null` body 视为空对象。
- 未知 `/memory/*` 路径由 mux 的 `"/"` 兜底，返回 404 `{status:"error",message:"接口不存在"}`，与 REST 其它错误同形（不用 Dashboard 的 `{code,data,message}` 信封，避免同一客户端按路径分支解析）；已注册路径的非 POST 仍由处理器返回 405 `{status:"error",message:...}`。
- `add` 走与 MCP 相同的 worker：入队成功就回 200（`deferred:true`），抽取与上游改写由后台继续。任务失败会落到 `failed`，在控制台的死信页可见、可重放；请求行与任务行同 `call_id`，两处能对上。
- 不实现 GET；所有端点只接受 POST。

## 测试与验证

- `httptest`：5 条成功路径、401、400 非法 JSON、415、上游错误映射、审计写入。
- OpenAPI：5 条路径的请求与响应同时过 `openapi3filter` 校验。
- 第 2 层验证：与 MCP 工具一起冒烟（G6）。

## 参考来源

- `docs/ARCHITECTURE.md` 的“入站协议”。

## 实现记录

See the public version-control history.
## 个人助手能力补齐（2026-10-31，accepted）

- 新增 `update_memory` / `POST /memory/update`，只接受memory_id、content与可选title；身份与作用域来自凭据。控制面先查归属，调用已有 `/product/update_memory` 后读回验证正文、标题与版本，不把受理当更新成功。重复相同正文不递增版本，禁止删除再新增模拟修改。
- 更新复用底座的嵌入重算和history，返回真实记忆投影供聊天日志展开。当前项目外、空正文、已删除或不存在记录必须拒绝。
- 个人助手默认开放六个记忆工具，具体删改仅按当前用户明确意图；检索内容不构成授权。环境仍可覆盖工具白名单。纠正反馈需检查目标ID归属，成功回执不代表产生了修改。
- 回答评分仍独立持久化；用户可在回答旁明确输入纠正，作为用户消息进入已有聊天链路，绝不由点赞/点踩推断事实。
- 验证：服务归属/no-op/上游失败、适配器请求作用域与错误、MCP/REST契约、配置工具暴露，以及dev任务项目真实写查改版本删闭环。


记忆事件关联（2026-10-31）：成功响应附加call_id，对异步Add沿用首次jobID，其他工具采用入口生成的调用ID。将相同可信ID经memory.WithEventScope传入用例，形成真实结果/事件/工具历史的关联；该字段不用于鉴权。现有字段与错误格式保持，事件契约见memory-events模块。
