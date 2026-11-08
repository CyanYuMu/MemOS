---
module: upstream
packages:
  - internal/adapter/upstream
status: implemented
---

# upstream

数据面 memos-api 的 HTTP 客户端。所有上游怪癖在这一层挡住，service 只看到干净的领域类型。

## 职责与边界

- 负责：HTTP 调用、超时、响应体积上限、信封与 `data.status=failure` 判定、错误映射、buckets 展平/去重/过滤/排序/截断、时间口径归一。
- 不负责：租户解析（identity 提供 `domain.Caller`）、业务规则（memory）、审计（transport/audit）。
- 租户条件：调用方（memory）把 `TenantID`/`UserID` 放进请求结构，adapter 每次都显式写入 `user_id` 与 `mem_cube_id`，不接受空租户。

## 依赖

- 依赖 `internal/domain` 与标准库 `net/http`、`encoding/json`、`io`。
- service 以使用方接口依赖它；adapter 不 import service。

## 契约

```go
type Client struct{ /* ... */ }

func New(opts Options, log *slog.Logger) (*Client, error)

func (c *Client) Add(ctx context.Context, req domain.AddPayload) (domain.AddResult, error)
func (c *Client) Search(ctx context.Context, q domain.SearchQuery) (domain.SearchResult, error)
func (c *Client) Profile(ctx context.Context, q domain.ProfileQuery) (domain.ProfileResult, error)
func (c *Client) Health(ctx context.Context) (map[string]any, error)
func (c *Client) Dashboard(ctx context.Context, q domain.DashboardQuery) (domain.DashboardPage, error)
func (c *Client) OwnedMemory(ctx context.Context, tenantID, userID, memoryID string) (domain.Memory, bool, error)
func (c *Client) OwnedMemories(ctx context.Context, tenantID, userID string, ids []string) (map[string]bool, error)
func (c *Client) Owners(ctx context.Context, ids []string) (map[string]string, error)
func (c *Client) Delete(ctx context.Context, tenantID string, ids []string) error
func (c *Client) Feedback(ctx context.Context, req domain.FeedbackRequest) (domain.FeedbackResult, error)
func (c *Client) GetExtractPrompt(ctx context.Context, tenantID string) (domain.ExtractPrompt, error)
func (c *Client) SaveExtractPrompt(ctx context.Context, tenantID, prompt string) error
func (c *Client) ResetExtractPrompt(ctx context.Context, tenantID string) (domain.ExtractPrompt, error)
func (c *Client) GenerateExtractPrompt(ctx context.Context, tenantID, description string) (string, error)
func (c *Client) DebugExtract(ctx context.Context, tenantID string, messages []domain.PromptMessage, types []string) (domain.ExtractDebug, error)
func (c *Client) Models(ctx context.Context) ([]domain.ModelInfo, error)
func (c *Client) ListProfileTemplates(ctx context.Context, tenantID string, page, size int) (domain.ProfileTemplatePage, error)
func (c *Client) GetProfileTemplate(ctx context.Context, tenantID, id string) (domain.ProfileTemplate, error)
func (c *Client) AddProfileTemplate(ctx context.Context, tenantID, name string, metadata map[string]any) (string, error)
func (c *Client) UpdateProfileTemplate(ctx context.Context, tenantID, id string, in domain.ProfileTemplateUpdate) (string, error)
func (c *Client) DeleteProfileTemplate(ctx context.Context, tenantID, id string) error
func (c *Client) KnowledgeBases(ctx context.Context, tenant string) ([]domain.KnowledgeBase, error)
func (c *Client) KnowledgeBase(ctx context.Context, tenant, id string) (domain.KnowledgeBase, error)
func (c *Client) CreateKnowledgeBase(ctx context.Context, tenant, name, description string) (domain.KnowledgeBase, error)
func (c *Client) UpdateKnowledgeBase(ctx context.Context, in domain.KnowledgeBaseUpdate) (domain.KnowledgeBase, error)
func (c *Client) DeleteKnowledgeBase(ctx context.Context, tenant, id string) error
func (c *Client) KnowledgeFiles(ctx context.Context, in domain.KnowledgeFilesQuery) (domain.KnowledgeFilePage, error)
func (c *Client) AddKnowledgeFiles(ctx context.Context, in domain.KnowledgeFilesAdd) ([]domain.KnowledgeFile, error)
func (c *Client) DeleteKnowledgeFiles(ctx context.Context, tenant string, fileIDs []string) (int, error)
func (c *Client) DownloadKnowledgeFile(ctx context.Context, tenant, fileID string) ([]byte, string, error)
func (c *Client) Contract(ctx context.Context) (Contract, error)
func (c *Client) CheckContract(ctx context.Context) error
func (c *Client) Ping(ctx context.Context) error
func (c *Client) GraphMemories(ctx context.Context, tenant string, types []string, limit int) (domain.GraphPage, error)
func (c *Client) EventMemories(ctx context.Context, in domain.EventMemoriesQuery) ([]domain.GraphMemory, int, error)
func (c *Client) MemoryBody(ctx context.Context, tenant, id string) (domain.GraphMemory, error)
```

租户字段（`TenantID`、`UserID`）放在请求结构里，由 memory 从 `domain.Caller` 填充；adapter 每次都显式写入 `user_id` 与 `mem_cube_id`。

### 端点与 payload

| 方法 | 端点 | 关键字段 |
|------|------|----------|
| Add | `POST /product/add` | `user_id`、`mem_cube_id`、`session_id`、`messages`、`async_mode:"sync"`、`custom_tags` |
| Search | `POST /product/search` | `user_id`、`mem_cube_id`、`query`、`top_k`、`include_dream_memory`、`dream_mem_top_k`、`filter`、`session_id`、`include_sources:false` |
| Profile | `POST /product/get_memory` | `user_id`、`mem_cube_id`、`page`、`page_size`、`include_preference`、`include_tool_memory`、`include_sources:false` |
| Dashboard | `POST /product/get_memory_dashboard` | `mem_cube_id`、`page`、`page_size`、`include_preference`/`include_tool_memory`、`include_sources:false`、`filter`；**流式解码**，见下 |
| Owned | `GET /product/get_memory/{id}?mem_cube_id=<tenant>&include_sources=false` | 单条查询并带租户条件 |
| Owned 批量 | `POST /product/get_memory_by_ids?mem_cube_id=<tenant>` | 契约 v2 对象体 `{"memory_ids":[...]}` |
| Delete | `POST /product/delete_memory` | `memory_ids` 与 `writable_cube_ids:[<tenant>]`；**绝不传 `user_id`** |
| Feedback | `POST /product/feedback` | `user_id`、`mem_cube_id`、`history`、`feedback_content`、`retrieved_memory_ids`、`async_mode:"sync"` |
| Prompt 读/写/重置 | `POST /product/get_extract_prompt`、`/save_extract_prompt`、`/reset_extract_prompt` | `mem_cube_id`；保存时为完整 prompt，必须含 `${conversation}` |
| Prompt 生成 | `POST /product/generate_extract_prompt` | `mem_cube_id`、`description`；不保存 |
| Prompt 干跑 | `POST /product/debug_extract` | `mem_cube_id`、`messages`、`extraction_types`；不写图库/向量库 |
| Models | `GET /product/models` | 无参数；上游从自己进程的环境变量生成 |
| Graph | `POST /product/get_memory_graph` | `mem_cube_id`、`memory_types`、`status:["activated"]`、`limit`、`label_max:200`、`include_edges:true`；一次返回节点与 `PARENT`/`MERGED_TO` 边 |
| Events | `POST /product/get_event_memories` | `mem_cube_id`、`memory_types`、`status:["activated"]`、`page`、`page_size`、可选 `search_query` |
| Graph body | `GET /product/get_memory/{id}?mem_cube_id=<tenant>` | 按 id 取正文，同时带回 `metadata.created_at`、`metadata.user_id`、`metadata.memory_type`；上游按 `mem_cube_id` 过滤 |

### 契约 v2 与自检

上游从 2026-10-23 起公布机器可读契约：`GET /product/contract` 返回
`contract_version`、`time.basis`、`sources.default`、`get_memory_by_ids.body`、`top_k_max`。
完整语义见上游 `docs/api-contract.md`；控制面只认 `ContractVersion = 2`。

- `CheckContract` 注册在 `/readyz` 的 `upstream` 项：版本、时间口径、sources 默认值、`top_k_max`
  任一项不符都返回 503，控制面不接流量。旧镜像没有这个端点（404/405）时按 `ErrContractUnsupported`
  放行，控制面继续按已兼容的旧形状工作；一旦端点存在，任何字段不符仍然拒绝。
  用意是让“上游换版本 → 控制面按旧口径静默算错”变成“启动即失败”。
- 上游实现契约端点（`contract_version=2`）后，控制面严格校验；还没实现的旧镜像返回 404，按“未声明契约”放行（`ErrContractUnsupported`），此时控制面仍按已兼容的旧形状工作。
- **升级到上游 v2 必须先上游、后控制面**：否则控制面会按 UTC 口径解析旧镜像的假 UTC 时间，出现时区偏移。
- 升级上游时先看 `/product/contract` 的 diff。破坏性变更会升 `contract_version`，控制面按需跟进；
  新增字段不影响解析，`Contract` 只解析控制面依赖的那几项。

### 仍然需要逐条遵守的约定

契约管住的是“语义”，下面这些是当前实现的具体形状，仍在 adapter 层集中处理：

1. **时间一律是真 UTC**：`domain.Memory.CreatedAt/UpdatedAt` 是 `domain.UTCTime`，
   不再按记忆类型挑 `ParseNaive`/`ParseUTC`，也不做任何偏移换算。Dashboard 的北京时间只是展示时区。
   历史节点由上游在读写边界归一（见上游 `docs/api-contract.md` 第 1 节），控制面看不见两种口径。
2. **`metadata.sources` 默认不返回**：调用方显式传 `include_sources:false`（不是依赖上游默认值，
   代码里能直接看到依赖）。仍然保留响应体上限：旧的膨胀数据与 `include_sources:true` 的请求
   都可能很大，`io.LimitReader(maxResponseBytes+1)` 超限返回 `ErrUpstreamRejected` 并记 WARN；
   `domain.Memory` 不含 `sources`。Dashboard 走流式白名单，见下一节。
3. **批量读取发 v2 对象体**：`get_memory_by_ids` 的 body 是 `{"memory_ids":[...]}`，`mem_cube_id` 是 query。
   裸数组 `["id",...]` 只是上游为过渡期保留的旧形状（`contract.py` 里标着 `accepts_legacy_array`），控制面不再发；`OwnedMemories` 一次批量取回并做归属校验，不再逐 id 发 GET。
4. **删除带租户作用域，但不传 `user_id`**：`user_id` 不是删除作用域，旧上游收到会静默失败。
   2026-10-24 起上游要求共享图库按 id 删除时显式给 `writable_cube_ids`，否则返回
   “writable_cube_ids is required when deleting by memory_ids”；控制面统一传 `[caller.TenantID]`。
   任何 `data.status=="failure"` 仍然一律当 `ErrUpstreamRejected`（新上游把参数错误改成 HTTP 400，
   这条是纵深防御）。
5. **`top_k` 按桶生效**：展平后每类分别截断到 `top_k`，跨桶不共享配额。
6. **`top_k` 上限 25**：契约 `top_k_max=25`，`MEMOSD_MAX_TOP_K` 的默认值与校验上限都已收紧到 25；
   超过会让整个检索请求 422，而不是少返回几条。
7. **检索返回软删除记录**（`metadata.status=="deleted"`）：过滤掉。
8. **同一条记录可能重复返回**：按 `id` 去重。
9. **上游无鉴权**：不发送任何 `Authorization`，也不把上游地址写进返回给调用方的错误。
10. **检索 `data` 是异构对象**：除 `text_mem`/`pref_mem`/`tool_mem`/`dream_mem` 外还有
    `act_mem`、`para_mem`、`pref_note`、`pref_string` 等辅助键，形状不一定是记忆组。
    只解析四个已知桶，其余忽略；否则整次检索会被判成“无法解析”，或把辅助桶误当 text 事实。
11. **图谱与事件记忆接口是 dumemos 2026-10-23 新增的**，旧镜像没有这两个端点，缺失时 404 被映射为
    `ErrUpstreamDenied`，Dashboard 返回 502（不重试）。与第 1 条的部署顺序同一条要求。
12. **`get_event_memories` 的 `search_query` 是大小写敏感的子串匹配**（上游 `get_all` 的 `memory.like`），
    与迁移前 Cypher 的 `toLower(...) CONTAINS toLower(...)` 不同。英文大小写会影响命中；中文不受影响。
13. **图谱与事件记忆的 `limit`/`page_size` 上限**分别是 10000/200；`MEMOSD_GRAPH_MAX_NODES` 的允许范围
    已收敛到 1..10000。节点标签按 `label_max=200` 截断，响应里的 `memoryTruncated` 透传给前端；
    正文仍按 id 另取。
14. **检索没有 offset，且 `filter` 与向量预过滤不在同一阶段**：`APISearchRequest` 只有 `top_k`（≤25，按桶生效），
    没有分页参数。Dashboard 检索页签把 `status`/`memory_type`/`user_id` 放进请求 `filter`，但上游的实际顺序是：
    先在向量库按 scope→`memory_type`、`status`、`user_name` 预过滤并取回最多 `top_k` 个候选，
    再在 Cypher 里对这批候选应用请求 `filter`（`n.id IN $vec_ids` + 条件）。后一步只能缩小候选集，
    不是“先过滤后截断”，最终条数可能少于 25，`total` 是本次命中的条数而不是全量匹配数。
    本地 `matchesFilter` 只是旧镜像忽略 `filter` 时的同条件兜底，不改变“上游无 offset、结果可能不足一页”的已知限制。
    上游提供分页参数前，不要在前端承诺可翻页；届时只改 Dashboard 的 `searchPage` 与 `Client.Search`。

### Dashboard 列表的流式解码

`get_memory_dashboard` 的 `pref_mem` 桶一页实测约 148 MB（`metadata.sources` 占 95% 以上，个别过滤条件下到 508 MB）。其余方法的“整包读入 + 8 MiB 上限”在这里不适用，所以 `Dashboard` 单独走流式路径：

- **选型**：Go 1.27 标准库 `encoding/json/v2` 与 `encoding/json/jsontext`（无需实验开关）。不手写解析器，也不引入第三方依赖：
  - v1 的 `json.Decoder.Decode` 会先把整个顶层值读进内部缓冲再解析，148 MB 会整块驻留，不适用；
  - `gjson`、`fastjson`、`jsonparser` 要求整包在内存，`sonic`、`jsoniter` 引入额外依赖且没有比标准库更好的流式跳过能力；
  - v2 的 `UnmarshalRead` 边读边解码，目标结构里没有的成员（`sources`、`embedding`、`history`、`internal_info`）由 `jsontext.Decoder` 逐 token 跳过，不物化，内存只与保留下来的白名单字段成正比。
- **白名单投影**：只解码 `id`、`memory`，以及 `metadata` 里的 `memory_type`、`status`、`created_at`、`user_id`、`mem_cube_id`/`cube_id`、`key`、`skill_name`、`skill_url`/`oss_url`、`template_id`/`profile_template_id`、`profile_instance_id`。`domain.DashboardRow` 不含 `sources`。
- **体积护栏**：原始响应上限固定 512 MiB（`maxDashboardBytes`，只防“上游无限吐”），超限返回 `ErrUpstreamRejected` 并中止读取；不是静默截断。HTTP 非 2xx 只读前 64 KiB 用于错误映射。
- **租户条件**：请求带 `mem_cube_id`；响应里的分组 `cube_id` 或记忆的 `mem_cube_id`/`cube_id` 与请求租户不符时丢弃（上游在 multi-DB 下可能忽略 `mem_cube_id`，fail-closed）。
- **形状**：桶是 `[{cube_id, memories:[{id,memory,metadata}], total_nodes}]`；`total` 取 `statistics` 里随 filter 计算的对应键（`total_text_nodes`/`total_preference_nodes`/`total_tool_nodes`/`total_skill_nodes`），不用 `total_nodes`（它等于本页条数）；缺失时退化为本页条数。按 `id` 去重，软删除（`status=="deleted"`）过滤。

### 图谱与事件记忆（2026-10-23 起走 HTTP）

`internal/adapter/upstream/graph.go` 实现 Dashboard 的图谱端口，控制面不再连接 Neo4j：

- **一次取回节点与边**：`GraphMemories` 调 `get_memory_graph`，把 `nodes` 与 `edges` 同时映射为 `domain.GraphPage`。上游已经丢弃端点不在本页的边，Dashboard 的 `buildGraph` 再做一次过滤，防止前端画出悬空边。
- **分页口径**：`GraphMemories` 的 `limit` 夹到 1..10000，`EventMemories` 的 `page≥1`、`page_size` 夹到 1..200；事件记忆的总数取上游 `total`，图谱的 `truncated` 取上游同名布尔值。
- **状态固定为 `activated`**：两条路径都显式传 `status:["activated"]`，保持迁移前只读 `activated` 节点的口径。Dream diary 使用 `completed`/`context_only`，当前事件记忆页签不含 `DreamDiary` 类型，因此不会被带入。
- **主体字段语义变化**：迁移前直读图库时 `userId` 取的是 `m.user_name`（即租户 cube）；`get_event_memories`/`get_memory_graph` 返回 `metadata.user_id`（记忆的真实归属主体）。Dashboard 的 `externalUserId`/`subjectType` 因此能区分租户自身与终端用户；字段为空时仍回退到租户。
- **正文归属**：`MemoryBody` 依赖上游 `get_memory/{id}?mem_cube_id=` 的过滤；若响应带 `mem_cube_id`/`cube_id` 且与请求租户不一致，适配器按“不存在”处理（fail-closed）。同一次响应同时映射 `metadata.created_at`（真 UTC，缺失时零值/空串）、`metadata.user_id` 与 `metadata.memory_type` 到 `domain.GraphMemory`，Dashboard 的事件记忆详情因此不用再打列表接口。**时间字段只影响展示**：`created_at` 解析失败不返回错误，`CreatedAt` 退化为空串，正文与主体照常返回；列表走 `domain.BeijingClock`，脏值同样原样/空串展示，不会出现“列表能看、详情 502”。
- **错误与体积**：复用通用 `post`/`do` 路径，因此响应体积上限、超时、404/5xx 映射与其他上游调用一致；不再有独立的 Neo4j 认证错误。

### 知识库（cloud 信封 + 租户隔离）

知识库文件与元数据都在上游：`get/create/update/delete_knowledgebase`、`add/get/delete_knowledgebase_file`、`download_knowledgebase_file`。全部按 `mem_cube_id` 隔离，成功 `code=0`、不存在 `code=404`（HTTP 仍是 200），复用模板接口的 `postCloud`/`checkCloudEnvelope`。

- 列表在上游只有 `page`/`page_size`，没有名称过滤：`KnowledgeBases` 按 200 条一页最多扫 20 页，名称过滤与最终分页在控制面做。
- `update_knowledgebase` 是本次为控制台补的端点；`name`/`description` 为 nil 表示不改，空名称由上游拒绝。
- 文件内容透传 cloud 的 `content`（URL、data URL 或裸 base64）；默认部署不拉取远程 URL（`KB_FETCH_REMOTE_FILES=false`）。
- 下载接口成功时没有信封，直接是文件流；控制面按 `Content-Disposition` 取原名并限制响应大小。

### 画像模板（另一套信封语义）

`/product/{list,get,add,update,delete}_profile_template` 与其余 `/product/*` 的信封语义不同：成功是 `code: 0`，不存在是 `code: 404`（HTTP 仍是 200），所以用 `doChecked` 换一套校验，而不是复用 `code == 200` 的默认判定。

- 租户边界：每个请求都带 `mem_cube_id=<已授权项目>`，上游按它校验模板归属；别的项目的模板返回 404，与不存在无法区分。
- 更新：只带需要改的字段。仅改名时不带 `metadata`，否则上游会用空树覆盖原属性树。
- 删除：上游把已绑定的实例置为 `detached`，不级联删除实例。
- 控制面不落库：模板与绑定实例的唯一权威是上游。

### 错误映射

| 上游情况 | 返回 |
|----------|------|
| 网络错误、超时 | `domain.ErrUpstreamUnavailable`，消息“暂时连不上记忆服务，请稍后重试。” |
| HTTP 5xx | `domain.ErrUpstreamUnavailable` |
| HTTP 401/403/404/409 | `domain.ErrUpstreamDenied`（控制面侧问题：凭据、路由或资源缺失，重试不会自愈） |
| HTTP 429 | `domain.ErrRateLimited` |
| HTTP 400/422 | `domain.ErrInvalidInput`，消息取上游 `message` 但截断到 300 字符 |
| 其它非 2xx | `domain.ErrUpstreamUnavailable` |
| HTTP 200 且 `code != 200` | `domain.ErrUpstreamRejected` |
| HTTP 200 且 `data.status=="failure"` | `domain.ErrUpstreamRejected` |
| 响应体超限 | `domain.ErrUpstreamRejected` |

`ErrUpstreamDenied` 是给控制面内部看的“永久失败”信号：worker 据此不重试；对外仍按内部错误收口（REST/Dashboard 502、MCP 统一“内部错误”），**不伪装成调用方凭据错误**，避免客户端误以为自己该换 token。`invalid_input`/`not_found`/`conflict` 等调用方可修正的错误另有映射，不受影响。

不自动重试：搜索本身 p99 可达 157s，重试会把上游负载翻倍；异步写入的重试由 worker 按任务状态决定。

## 错误与边界情况

- `baseURL` 去掉尾部 `/`；空地址在构造时返回错误。
- 请求一律 `context` 化；`http.Client` 显式设置 `Timeout`，不使用 `http.DefaultClient`。
- 响应 JSON 解析失败 → `ErrUpstreamRejected`；`data` 为 null 时按空结果处理。
- `OwnedMemory` 只返回 `(memory, true, nil)` / `(zero, false, nil)` / 错误；(false) 同时覆盖不存在、已删除与不属于该租户。元数据有 `mem_cube_id` 时精确匹配租户；缺失租户时用 `user_id` 兜底；两者都无法证明归属时 fail-closed。
- `Owners` 调 `/product/get_user_names_by_memory_ids`，仅用于删除前区分 missing 与 foreign；响应不是对象时返回错误，调用方不得删除。这是本客户端**唯一**不带租户条件的读取，属于与网关 `owner_of` 同形的有意例外，只返回 `user_name`（即 cube_id），不返回正文。
- `Search` 对空 `query` 直接返回 `ErrInvalidInput`，不打上游。
- `Dashboard` 的 `PageSize` 夹取到 1..100，`Page` 最小 1；`Bucket` 不是四个桶之一时返回 `ErrInvalidInput`。`Filter` 原样透传（调用方负责 `status:"activated"` 等口径）。

## 测试与验证

- `httptest` 假上游：展平/去重/过滤/排序/截断、`+08:00` 换算成 UTC、超限响应、`data.status=failure`、HTTP 5xx/超时映射、删除 payload 不含 `user_id`、单条 GET 带 `mem_cube_id`。
- 契约自检：匹配时通过；端点 404/405 视为旧镜像放行；`contract_version` 不符、时间口径/sources 默认值/`top_k_max` 变化、上游挂起超时都会失败，且耗时不超过就绪探针预算（`contract_test.go`）。
- 图谱与事件记忆：节点/边映射、`memoryTruncated`、`page`/`page_size` 与 `limit` 夹取、`status=activated` 与租户参数、正文不属于租户时 not found、404 映射为 `ErrUpstreamDenied`、正文响应映射创建时间/主体（缺失或非法 `created_at` 时为空，且不影响正文返回）。
- 检索过滤：`Filter` 原样出现在 `/product/search` 请求体的 `filter` 里，`top_k` 不超过契约上限。
- golden 不需要：这是内部 adapter，对外契约在 mcp/dashboard。
- 本地单元测试；第 2 层验证用真实上游冒烟（见 memory 模块）。

## 参考来源

- `docs/ARCHITECTURE.md` 的“上游约束”。

## 实现记录

See the public version-control history.
## 记忆详情扩展（2026-10-31，implemented）

详情读取显式include_sources=true，传递metadata.version/history/sources。历史兼容对象和JSON字符串，坏条目不使整条记忆无法读取，保留实际版本号/正文/原因/时间，不从当前内容伪造旧版本。普通检索与活动仍include_sources=false，详情独立方法避免扩大列表响应。

## 个人助手能力补齐（2026-10-31，accepted）

- 新增 `update_memory` / `POST /memory/update`，只接受memory_id、content与可选title；身份与作用域来自凭据。控制面先查归属，调用已有 `/product/update_memory` 后读回验证正文、标题与版本，不把受理当更新成功。重复相同正文不递增版本，禁止删除再新增模拟修改。
- 更新复用底座的嵌入重算和history，返回真实记忆投影供聊天日志展开。当前项目外、空正文、已删除或不存在记录必须拒绝。
- 个人助手默认开放六个记忆工具，具体删改仅按当前用户明确意图；检索内容不构成授权。环境仍可覆盖工具白名单。纠正反馈需检查目标ID归属，成功回执不代表产生了修改。
- 回答评分仍独立持久化；用户可在回答旁明确输入纠正，作为用户消息进入已有聊天链路，绝不由点赞/点踩推断事实。
- 验证：服务归属/no-op/上游失败、适配器请求作用域与错误、MCP/REST契约、配置工具暴露，以及dev任务项目真实写查改版本删闭环。





Content corrections invalidate content-derived tags in the actual upstream metadata (tags=[]), while title-only/no-op updates preserve tags. The trusted memory service determines whether content changed; no caller-supplied metadata or ownership override is accepted. Historical before snapshots keep their original tags. The existing upstream update metadata capability is reused without changing or deploying the shared data plane.


Implementation validation: goal5-history-before.log reproduced the two frontend historical lookup/retention failures; goal5-history-fe-verify.log passes 50 tests and frontend gates. goal5-history-be-tests.log passes service/store/upstream/dashboard/MCP race+shuffle cases, including tenant-specific old-call lookup beyond latest50, real metadata tag invalidation, preserved before tags, version-gap failure, serialized before-version chain, and cancellation while waiting. Retry failures preserve the feed cursor.

### P3 独立 Skill 服务

`SkillGateway.Call(ctx, domain.SkillCall)` 对固定 action 集合发出单次 POST `/v1/skills/{action}`，不与 Memory 客户端共享配置。仅服务端注入 Bearer 服务凭据与 X-Skill-Actor/Capability/Project，且 payload.projectId 必须和可信项目一致。禁止跟随重定向；30 秒超时、8 MiB 响应上限，无自动重试。上游 `{data:...}` 原样放回控制台 data；错误仅保留安全错误码和 currentSequence，私有 message/details/端点不透传。服务凭据 401 转 503，避免把服务器配置问题误报为浏览器会话过期。
