---
module: memory
packages:
  - internal/service/memory
status: implemented
---

# memory

6 个记忆用例：`add_message` / `search_memory` / `update_memory` / `delete_memory` / `add_feedback` / `get_user_profile`。所有用例的租户条件来自 `domain.Caller`，工具/HTTP 入参里没有 `user_id`。

## 职责与边界

- 负责：入参归一（消息展开、tool_calls 清洗、session_id 折算、top_k 截断）、租户收口、删除归属检查、异步写入决策、结果整形（去重/过滤后的领域结果）、调用审计的字段准备。
- 不负责：协议编解码（transport/mcp、transport/rest）、上游怪癖（upstream）、任务持久化（worker/store）、Token 解析（identity）。

## 依赖

- 依赖 `internal/domain`。
- 端口由本包声明，adapter 结构化实现：

```go
type Upstream interface {
    Add(ctx context.Context, req domain.AddPayload) (domain.AddResult, error)
    Search(ctx context.Context, q domain.SearchQuery) (domain.SearchResult, error)
    Profile(ctx context.Context, q domain.ProfileQuery) (domain.ProfileResult, error)
    OwnedMemory(ctx context.Context, tenantID, userID, memoryID string) (domain.Memory, bool, error)
    OwnedMemories(ctx context.Context, tenantID, userID string, ids []string) (map[string]bool, error)
    Owners(ctx context.Context, ids []string) (map[string]string, error)
    Delete(ctx context.Context, tenantID string, ids []string) error
    Feedback(ctx context.Context, req domain.FeedbackRequest) (domain.FeedbackResult, error)
}

type Jobs interface {
    EnqueueJob(ctx context.Context, j domain.Job) error
}
```

`Jobs` 只在 `AddAsync` 打开时使用；MCP `add_message` 与 REST `/memory/add` 都传 `Async=true`，入队即返回。

## 契约

```go
type Options struct {
    DefaultTopK int // 默认 10
    MaxTopK     int // 默认 25（上游契约 top_k_max）
    QueryPrefix string
    DreamTopK   int // 默认 2；0 = 不召回 dream
    AddAsync    bool
    Idem        Idempotency // nil 时不启用请求去重
    Observer    Observer
}

type Service struct{ /* ... */ }

func New(up Upstream, jobs Jobs, opts Options, log *slog.Logger) *Service

func (s *Service) Add(ctx context.Context, caller domain.Caller, req domain.AddRequest) (domain.AddResult, error)
func (s *Service) Search(ctx context.Context, caller domain.Caller, q domain.SearchQuery) (domain.SearchResult, error)
func (s *Service) Delete(ctx context.Context, caller domain.Caller, ids []string) (domain.DeleteResult, error)
func (s *Service) Feedback(ctx context.Context, caller domain.Caller, req domain.FeedbackRequest) (domain.FeedbackResult, error)
func (s *Service) Profile(ctx context.Context, caller domain.Caller, q domain.ProfileQuery) (domain.ProfileResult, error)
```

### 用例规则

- **Add**：`caller.Validate()` 后填充 `TenantID`/`UserID`；`SessionID = explicit || sha1(first_message)[:16]`（`conv_` 前缀）；消息归一：
  - `messages` 是 JSON 字符串或字符串数组时先解析成对象；
  - `content` 保留字符串或数组（多模态），其他类型字符串化；
  - 只保留 `role`、`content`，有 `tool_calls` 时清洗并保留 `id/type/function.name/function.arguments`，有 `tool_call_id`/`name` 时保留；
  - 归一后为空 → `ErrInvalidInput`。
  - `Async=true` 且 `AddAsync` 打开 → 写入 `jobs`（payload 是请求快照），返回 `status=success`、`message="Memory accepted. Extraction continues in the background; no need to wait."`、`deferred=true` 与 `call_id`（= job ID）；否则同步调用上游，返回 `"Memory added successfully"`。MCP 与 REST 入口都固定请求 `Async=true`（不阻塞 agent），只有队列不可用或 `AddAsync` 关闭时才退回同步。
  - **请求去重（自动）**：`Options.Idem` 已接线时，`Add` 在消息归一后按 `(tenant_id, 归一 payload 哈希, 同步/异步模式)` 自动去重，调用方不需要传键；REST 的 `Idempotency-Key`（或 body `idempotency_key`）仍作为显式覆盖，同键不同内容返回 `ErrConflict`。记录 TTL 由 worker 按 `IdempotencyTTL`（默认 24h）清理。
    - 同步写入成功 → `succeeded` + 首次响应，重复请求直接重放。
    - 异步写入 → 入队后 `accepted` + “已接收”响应，重复请求返回同一份 accepted，不重复入队；worker 终态回调：成功置 `succeeded`，永久失败释放记录，让同内容请求可以重新入队。
    - `AddResult.CallID` 是异步路径实际使用的 job ID（优先请求的 `call_id`，为空才生成），随幂等缓存一起保存；`accepted`/`succeeded` 重放原样带回该值，transport 用它覆盖重放请求的审计 `call_id`，重放请求因此能对上首次任务的终态行。同步路径为空。
    - 只保证请求边界去重，不是 exactly-once：上游 `/product/add` 没有幂等键，“上游写成功但响应丢失”后无法区分，同步路径与 worker 重试都可能重复写。彻底解决需要上游支持幂等键。
    - 代价：同一租户、同一会话、内容完全相同的两次合法写入会被合并（24h 内）；不同会话靠 `session_id` 区分。自动键包含 `Async` 开关，避免同一请求在异步/同步实例间被误判为重放；MCP 与 REST 现在同为异步，同一段对话从任一入口进来都只写一次。
- **Search**：`query` 为空 → `ErrInvalidInput`；`top_k` 夹到 `[1, MaxTopK]`，缺省 `DefaultTopK`；`DreamTopK` 来自配置，`include_dream_memory = DreamTopK>0`；`QueryPrefix` 非空时拼在 query 前；`session_id` 同上折算；上游返回已展平。
- **Delete**：`ids` 去空、去重；一次 `OwnedMemories(tenantID, userID, ids)` 批量校验归属：
  - 命中且属于自己 → 待删；
  - 未命中 → 记入 unknown，再用一次 `Owners(unknown)` 区分“不存在/已删除”与“别人的记忆”；
  - 归属查询失败 → 直接返回错误，不删除任何 id（fail-closed）；
  - 任一 id 的 owner 存在且不是当前 `TenantID` → **整批拒绝**（`ErrForbidden`），不调用上游删除；
  - 其余 unknown → `not_found_ids`；待删非空时一次 `Delete(tenantID, mine)`，上游要求 `writable_cube_ids:[tenant]`，控制面不传 `user_id`。与网关 `gateway/service.py` 一致：missing 不阻断自有删除，foreign 才整批拒绝。
- **Profile**：`size` 夹到 `[1, 50]`，`current ≥ 1`；返回各类记忆与 `total`。
- 所有用例在调用上游前 `caller.Validate()`；`caller.Admin` 不能调用记忆用例（返回 `ErrForbidden`）。

### 会话标识（session_id）

- 取值：显式 `session_id` 优先；缺失时 `conv_` + `sha1(first_message)[:16]`（`ResolveSessionID`）。
- 哈希回退是**内容寻址，不是会话寻址**：同一会话内首条用户消息不变，id 就稳定；不同会话只要首条消息相同，就会算出同一个 id。相似任务的开场白可能高度模板化，这种坍缩比哈希碰撞现实得多。
- 坍缩**不会**把两个会话的内容写混，影响面按上游用途拆开：
  - 写入：`session_id` 只写进抽取结果的 metadata（dumemos `core.py` 的 `info`），抽取内容只取本次请求的 `messages`，不拼接历史会话；
  - 检索：上游把它用作同会话加权（`search_priority`）、BM25 候选收窄（`id_filter` 硬过滤）与偏好更新的过滤条件。坍缩的后果是另一会话的同开场白写入参与加权或进入候选，属于召回偏差，不是数据错误。
- 结论：哈希回退只承诺“同一会话内稳定”，不承诺“跨会话唯一”。要精确寻址只能由调用方给 id——任何基于内容的确定性映射都无法区分内容完全相同的两个会话。
- 决策（2026-10-23，已落地）：MCP 侧已开放可选 `session_id` 入参（见 `mcp.md`），REST 已有；新接入方应显式传，哈希仅作旧客户端兼容。不采用时间桶哈希等启发式：同一天开场白相同仍会碰撞，且会切断跨午夜的长会话。
- 已知的上游行为（待确认）：BM25 路径把 `session_id` 当硬收窄而不是加权（`recall.py` 的 `id_filter`），合成 id 下跨会话记忆在这条召回路径上不可见。若召回质量受影响，应优先与上游确认该语义，而不是改本服务的折算规则。

### 不变量

- 上游调用必带租户条件；service 不接受请求结构里的 `TenantID`/`UserID` 覆盖 caller（构造请求时一律用 caller 的值）。
- 删除绝不跨租户；归属未知时拒绝删除，不把“查不到”当放行；`not_found_ids` 只包含确认不存在的 id。
- `TopK` 不放大到超过 `MaxTopK`；`size` 不超过 50。
- 返回给调用方的错误消息不包含上游地址、token 或记忆正文。

## 错误与边界情况

- 上游不可用 → `ErrUpstreamUnavailable`（transport 映射 503/500）；上游拒绝 → `ErrUpstreamRejected`；参数错误 → `ErrInvalidInput`。
- 异步入队失败 → 退回同步写入，不丢数据。
- 异步模式下上游失败只写任务状态与审计，调用方已经拿到“已接收”。这是有意的**尽力而为**语义：入队失败退回同步；worker 按 `MaxAttempts` 重试；永久失败写 `add_message.background` 审计行并释放请求去重记录，同内容重发可重新入队，但不回传调用方，也不提供任务查询接口。调用方要确认某次写入是否生效，只能通过 `search_memory` 侧证或控制台审计。

## 测试与验证

- 假 Upstream 捕获请求：租户字段来自 caller、`top_k` 截断、dream 开关、session_id 折算、消息展开与 tool_calls 清洗；异步路径只入队、不调用上游，响应带 `deferred`/`call_id`。
- 删除：混合归属整批拒绝（不删除任何 id）、不存在返回 `not_found_ids` 且自有 id 正常删除、全部属于自己时一次删除、归属查询失败时不删除。
- feedback 摘要与截断、profile 分页夹取。
- 第 2 层验证：本地进程 + `forward-upstream` 对 5 个工具与 REST 冒烟（G6 前半）。

## 参考来源


## 实现记录

See the public version-control history.
## 个人助手能力补齐（2026-10-31，accepted）

- 新增 `update_memory` / `POST /memory/update`，只接受memory_id、content与可选title；身份与作用域来自凭据。控制面先查归属，调用已有 `/product/update_memory` 后读回验证正文、标题与版本，不把受理当更新成功。重复相同正文不递增版本，禁止删除再新增模拟修改。
- 更新复用底座的嵌入重算和history，返回真实记忆投影供聊天日志展开。当前项目外、空正文、已删除或不存在记录必须拒绝。
- 个人助手默认开放六个记忆工具，具体删改仅按当前用户明确意图；检索内容不构成授权。环境仍可覆盖工具白名单。纠正反馈需检查目标ID归属，成功回执不代表产生了修改。
- 回答评分仍独立持久化；用户可在回答旁明确输入纠正，作为用户消息进入已有聊天链路，绝不由点赞/点踩推断事实。
- 验证：服务归属/no-op/上游失败、适配器请求作用域与错误、MCP/REST契约、配置工具暴露，以及dev任务项目真实写查改版本删闭环。


## 异步新增的历史快照（2026-10-31，accepted）


- `RunAddJob` 仅在上游写成功后，按该次返回的去重ID读取归属正确的记忆，保存 `MemoryWriteSnapshot` v1。包括租户、用户、会话、UTC采集时间及真实记忆实体投影；投影保留七字段与版本，历史与来源另走详情接口。该值以 `AddResult.Snapshot` 内部字段交给worker，不进入MCP/REST响应或幂等响应缓存。
- 快照读取总预算5秒（并服从原任务剩余期限）、最多50条、序列化正文预算128KiB；空快照信封自身超过预算时整体省略快照、保留确认ID输出；其余超限或不可读实体整体省略并标记 `truncated`，不截断字段伪造完整实体。确认ID数仍以去重后的真实上游结果计。
- 写入已经成功后，快照读取失败不能把任务改为失败或触发再次提取；保留已成功状态，日志只记安全操作名与数量。快照为空、缺项或超限时可见的 `truncated=true` 表示详情不完整。
- worker完成输出格式为 `{added_memory_ids: [...], snapshot?: {...}}`。v1快照是不可变审计结果，读取活动绝不再查询当前记忆。采集时拒绝实体中显式冲突的租户或用户归属；按审计租户/工具/call_id及快照租户/用户/ID白名单双重校验，内容失配不展示。
- 旧输出仅有ID而无快照时保留原数量、返回 `unavailable` 与空详情；不得用当前值回填历史。旧成功且确认空ID集合时可返回成功空集合。失败、等待、无关联保持原语义。

兼容与回滚：只给现有审计输出新增可选字段，无数据库迁移或对外响应形状变化。旧二进制能忽略新字段；切回旧镜像仍可读取原ID。旧二进制会再次读最新正文，因此仅在明确回滚期间接受该已知限制。
