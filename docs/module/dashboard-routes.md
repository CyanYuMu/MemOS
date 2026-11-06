---
module: dashboard-routes
packages:
  - internal/transport/dashboard
status: implemented
---

# dashboard-routes

Dashboard 的 `/api/dashboard/*`、`/api/meta`、`/api/health` 与 `/api/user/*` 路由清单与逐条状态。来源是 `memory-console/docs/BACKEND-INTEGRATION.md` §4（37 条声明：29 条主路径 + 8 条知识库）。本文件是核对表：37 条每条都可调用——已实现、由上游代理、或明确返回契约正确的 503/409；不存在静默假数据。

## 职责与边界

- 负责：route 级协议、参数解析、信封 `{code,data,message}`、身份（会话/零信任/管理 Token）、把请求编排到 memory/upstream、store、identity。
- 不负责：真正的记忆/图谱/知识库/模板存储（都在上游）、按知识库召回记忆（上游没有该接口）、切流。

## 依赖

- `internal/service/memory`、`internal/service/identity`、`internal/service/audit`、`internal/transport/httpx`。
- 端口（使用方接口，adapter 结构化满足）：`Tenants`（service/tenant）、`Tokens`（identity）、`Memories`（upstream 的 `OwnedMemory`、`Dashboard`）、`Templates`（upstream 的画像模板五个方法）、`ConfigMaps`（k8s）、`Graph`（upstream 的图谱/事件记忆/正文四个方法）。

## 契约

```go
type Deps struct {
    Tenants        Tenants        // service/tenant：项目创建/改名/启停/归档/恢复
    Tokens         Tokens         // identity：Key 生命周期
    Memory         *memory.Service
    Memories       Memories       // upstream：记忆列表/详情
    Templates      Templates      // upstream：画像模板五个方法
    Prompts        Prompts        // upstream：抽取 prompt 五条
    Models         Models         // upstream：模型清单
    Accounts       Accounts       // account：Authenticate、SwitchProject、MemberRoles
    Knowledge      Knowledge      // upstream：知识库与文件
    Audit          *audit.Recorder
    Graph          Graph          // upstream：图谱/事件记忆/正文
    Service        string
    AuthMode       string         // /api/meta 的 page | external
    GatewayURL     string         // 同进程 MCP 回环地址；console 单独部署时为空
    GraphMaxNodes  int
    InvalidateAuth func()
    Logger         *slog.Logger
}

func New(deps Deps) (*Server, error)
func (s *Server) Handler() http.Handler // 挂到 console 角色的 /api/
```

- 鉴权：由 `transport/auth` 的 `Rule` 统一裁决，凭据是会话 cookie（`memos_console_sid`）或管理 Token（Bearer，平台管理员等价）。未登录的 `/api/*` 返回 401 `{code:40100,message:"登录已过期，请重新登录"}`；非成员 403；角色矩阵见 `accounts.md`。唯一的匿名例外是 `/api/meta`，它只返回公开子集（`authEnabled`/`authMode`），登录后才带 `sources`；`authMode` 默认 `"page"`，零信任必选时为 `"external"`（前端据此隐藏退出入口）。
- 当前项目：每个会话保存一个“当前项目”（`project/update current=true` 切换，只影响本会话；`status=ENABLED` 是恢复停用项目，不切换）。前端的 `memory/detail`、`memory/delete`、`log/detail`、`keys/copy` 不带 `projectId`，所以项目按 body.projectId → query.projectId → 会话当前项目依次解析，解析结果经授权后写回请求体再交给 handler。
- 成功：HTTP 200 + `{code:0,data,message:"success"}`；失败：`httpx.Fail`，`data` 为 null，`code` 与状态对应：`40000` 参数/`40100` 未登录/`40300` 无权限/`40001` 不存在/`40900` 冲突/`41300` 请求体或正文超限/`42900` 限流/`50143` 上游拒绝/`50300` 能力未启用或上游不可用/`50000` 内部错误。
- 未知 `/api/dashboard/*` / `/api/*` 路径由 dashboard mux 的 `"/"` 兜底，返回 404 JSON 信封 `{code:40001,data:null,message:"接口不存在"}`，不会落到 SPA 或纯文本 404；已注册路径的方法不匹配返回 405 JSON 信封 `code=40500` 并保留 `Allow` 头（`httpx.MethodNotAllowedJSON` 包装各入口）。dashboard 的 429 来自上游限流，控制面不知道确切的恢复时间，因此**不写 `Retry-After`**；只有登录失败节流的 429（`transport/auth`）带该头。
- 时间：上游契约 v2 的时间一律是真 UTC，记忆列表/详情只做北京时间展示转换（`createTime` 为 `YYYY-MM-DD HH:mm:ss`）；项目的 `createTime` 仍按 ISO 输出。
- 审计来源：日志列表支持 `source` 过滤（`mcp`/`plugin`/`rest`/`dashboard`/`admin`/`auth`），与 `eventType` 是两个维度；`scope=gateway|console` 仍按工具名互补过滤。

### 29 条主路径逐条状态

| # | 前端路径 | 数据来源 | 状态 | 说明 |
|---|----------|----------|------|------|
| 1 | `POST /api/dashboard/project/memory/list` | 上游 `get_memory_dashboard`（流式）+ 上游图谱/事件记忆 | 已实现 | 5 个页签：`text_mem`/`pref_mem`/`tool_mem` 走上游桶（服务端过滤 + 真分页，总数取 `statistics`），`profile_mem`（UserMemory）走 `get_memory_graph`，`event_mem`（EventMemory/Context/InsightMemory）走 `get_event_memories`；带搜索词时桶页签走上游检索：`status`/`memory_type`/`user_id` 经 search 的 `filter` 下推，但上游是“向量库按 scope→`memory_type`/`status`/`user_name` 预过滤取候选，再在 Cypher 里对候选应用请求 `filter`”，属于候选后过滤而不是同一阶段的 pre-filter，最终结果可能少于 25 条；`top_k` 取契约上限 25/桶，本地按同一条件兜底过滤后再切片。**已知限制**：上游 `/product/search` 没有 offset，每桶最多返回 25 条，检索结果里的 `total` 是本次命中的条数而不是全量匹配数，超过 25 条的匹配无法翻页；检索分页收敛在 dashboard 的单一函数 `searchPage`，上游提供 offset 后只改这里与适配层。事件记忆页签把搜索词交给上游做正文子串匹配（大小写敏感）。`knowledgeBaseId` 非空返回 503（上游没有“按知识库召回记忆”的接口），页签未知返回 400（不再静默空列表）；`startTime`/`endTime` 忽略；上游缺少接口或不可达返回 503（与 `memory/detail`、图谱一致，不伪装成空数据）。`createTime` 为 `YYYY-MM-DD HH:mm:ss` 北京时间：上游契约 v2 的时间一律是真 UTC，这里只做展示时区转换（不再按类型分支）。148 MB 级一页用 `encoding/json/v2` 流式解码，不 OOM（见 upstream.md）。 `subjectType` 是分页后的本页过滤（上游没有该字段，按 user_id 与租户的关系推），此时 `total` 仍是过滤前的总数；当前前端只展示该列、不发这个条件。 |
| 2 | `POST /api/dashboard/project/memory/detail` | 上游单条 GET（`OwnedMemory`） | 已实现 | 单条仍走 8 MiB 上限；不属于租户按 404；`createTime` 与列表同口径。`event_mem` 详情复用同一次 `GET /product/get_memory/{id}` 响应里的 `metadata.created_at` 输出 `createTime`（北京时钟，与列表一致），`externalUserId`/`subjectType` 按 `metadata.user_id` 与租户关系推（空值回退租户），不再硬编码 `agent`/projectId；上游缺 `created_at` 或值不符合 RFC3339 时 `createTime` 为空串，正文与主体照常返回（时间只影响展示，不让详情 502），不额外打列表接口。 |
| 3 | `POST /api/dashboard/project/memory/delete` | `memory.Service.Delete` | 已实现 | 逐条归属校验，跨租户整批拒绝（403），不存在返回 `not_found_ids`；删除只传 `memory_ids` 与 `writable_cube_ids:[项目]`，不传 `user_id`。 |
| 4 | `POST /api/dashboard/log/list` | `audit_records`（控制台自己的审计表，不读网关 JSONL） | 已实现 | 默认返回当前项目的全部审计行（网关 5 个对外工具 + `dashboard.*`/`admin.*`/`auth.*` 控制台操作），`scope=gateway` 只看对外工具、`scope=console` 只看控制台操作（`background` 终态行始终合并、不单列）；`source` 按调用入口补充过滤（`mcp`/`plugin`/`rest`/`dashboard`/`admin`/`auth`），与 `eventType` 正交；按项目过滤并要求 viewer；`eventType`/`status`/`query`（子串，不区分大小写；`query` 同时匹配同一 `call_id` 终态行的 `err`/`output`）/`startTime`/`endTime`（北京时间，精度分钟，含端点）/`subjectId` 都生效；`subjectId` 与 `subjectType` 无关，单独判定（不等于当前项目 ID 返回 400），只有 `subjectType=all` 才忽略它；`createTime` 为北京时间。异步 add 的请求行按 `call_id` 合并后台终态行：终态成功为 `SUCCESS`、终态失败为 `FAILED`；没有终态行时，只有同一 `call_id` 的 job 仍在库里才为 `RUNNING`，job 已被清理（默认保留 7 天）则按请求行自身 `ok` 回退为 `SUCCESS`/`FAILED`（升级前落库、`call_id` 与 job ID 对不上的历史行因此直接按行内 `ok`，不会永久“卡住”）。非 deferred 行按自身 `ok`；`total` 只计请求行（终态行不进入列表也不计数），`status` 过滤、分页与计数都在 SQL 内完成。响应键是 `logs`，不是 `records`；`truncated` 恒为 false。未知 `eventType`/`status`/`source`、非法时间格式、`subjectId` 与当前项目不一致都返回 400（`code=40000`），不再静默空列表；`eventType` 以 5 个网关事件为主，也接受完整的控制台工具名（`dashboard.*`/`admin.*`/`auth.*`）。列表行含 `callId`/`source`，详情含 `callId`/`requestId`/`errMessage`，`requestCode`/`responseCode` 取审计记录的真实 `http_status`（历史行按派生状态兜底）。 |
| 5 | `POST /api/dashboard/log/detail` | 同上 | 已实现 | 按日志所属项目判权（请求只带 `logId`）；不存在、无权限对非平台管理员统一 403，平台管理员对不存在返回 404；没有租户的审计行（登录等）仅平台管理员可见。`status` 与列表同口径（按 `call_id` 合并终态行），异步 add 的请求行不会出现列表 `FAILED`、详情 `SUCCESS` 的不一致；`requestBody` 是入参的缩进 JSON，`responseBody` 失败时是 `{ok:false,err}`（错误与正文优先取同一 `call_id` 的终态行），成功时是输出原文；`traceId` 取会话标识；异步 add 的请求行会合并终态行的 `requestTokens`/`responseTokens`/`llmCalls`，上游没有回报时不下发（前端留空，不显示 0）。 |
| 6 | `POST /api/dashboard/usage/query` | `audit_records` 聚合 | 已实现 | 按北京日期与事件类型统计调用次数，区间内逐天补齐（上限 400 天）；`addMessage.totalTokens` 来自异步 add 终态行的上游 usage；chat、extractMemory、rerank 恒为 0（没有可观测数据，不编数）。`projectId` 为空（前端“全部”）时取会话当前项目，不做跨项目汇总（决策点）；`apiKeyId` 忽略。 |
| 7 | `POST /api/dashboard/keys/list` | 控制面 tokens 表 | 已实现 | **只给前缀掩码**（`memos_xxxx…`），明文不随列表批量下发。`canCopy` 表示「点复制时能不能通过 `keys/copy` 取到明文」（项目写权限 + 库里留有明文）。不返回哈希；已吊销的 Key 不出现（吊销在前端等同删除）。另下发 `lastUsedAt`（从没用过则无该字段）。 |
| 8 | `POST /api/dashboard/keys/create` | `identity.IssueTokenWithExpiry` | 已实现 | 明文与哈希同时落库（迁移 `0024`）；**明文只在创建响应里进一次响应体**（`apiKey` 明文、`canCopy=true`），之后只能按 id 走 `keys/copy`。可选 `expireDays`（整数 1..3650，缺省或 0 = 永不过期）；非法值返回 400。响应多一个 `expireTime`（北京时间；永不过期为 `""`）。 |
| 9 | `POST /api/dashboard/keys/revoke/{id}` | `identity.RevokeToken` | 已实现 | 幂等；不存在 404。 |
| 9b | `POST /api/dashboard/keys/rename/{id}` | `identity.RenameToken` | 已实现 | 项目 editor。只改展示名 `label`（1..64 字符），哈希、前缀与过期时间不变，已接入的调用照常可用；不存在 404。 |
| 10 | `POST /api/dashboard/keys/copy` | tokens 表 | 已实现 | body `{apiKeyId}`；要求该项目**写权限**，返回 `{apiKey}`。库里没有明文（迁移 `0024` 之前签发、旧网关导入的 legacy Key）时返回 409 并提示重新生成。 |
| 11 | `POST /api/dashboard/prompt-config/detail` | 上游 `/product/get_extract_prompt` | 已实现 | 项目 viewer。返回 `{projectId,prompt,extractionTypes,updatedAt,source,isDefault}`；项目没保存过时上游返回内置默认 `SIMPLE_STRUCT_MEM_READER_PROMPT_ZH`。隔离靠 `mem_cube_id`（= 授权后的 `projectId`）。 |
| 12 | `POST /api/dashboard/prompt-config/save` | 上游 `/product/save_extract_prompt` | 已实现 | 项目 editor。`prompt` 必填且必须含 `${conversation}`（上游校验，否则干跑会没有对话内容）；存到上游 `MOS_CUBE_PATH/extract_prompts.db`，控制面不落第二份。 |
| 13 | `POST /api/dashboard/prompt-config/reset-default` | 上游 `/product/reset_extract_prompt` | 已实现 | 项目 editor。删除该项目保存的 prompt，返回内置默认值。 |
| 14 | `POST /api/dashboard/prompt-config/generate` | 上游 `/product/generate_extract_prompt` | 已实现 | 项目 editor。上游用自己配置的 chat 模型按 `description` 重写当前 prompt；**不保存**，由用户确认后再调 save。模型输出丢失 `${conversation}` 时上游返回 502。 |
| 15 | `POST /api/dashboard/prompt-config/debug/extract` | 上游 `/product/debug_extract` | 已实现 | 项目 editor。用项目当前 prompt 干跑一次，不写图库与向量库；返回 `{extractionTypes,results:[{type,content}]}`。模型没按 JSON 结构返回时给一条 `type:"raw"`，不伪装成抽取成功。 |
| 17 | `POST /api/dashboard/project/list` | tenants 表 | 已实现 | `{records,total}`；每行 `{id,name,description,status,createTime,isMultiView,kind,archived[,archivedAt],disabled,role,member}`。`role` 只报真实成员关系（平台管理员对非成员项目为空串），`member` 同口径；归档项目带 `archived:true` 与北京时间 `archivedAt`，仍返回给 owner/平台管理员恢复；停用项目仍返回给真实成员与平台管理员，带 `disabled:true` 与真实 `role`，owner 据此看到「启用」入口。 |
| 19 | `POST /api/dashboard/project/update` | tenants 表 | 已实现 | 支持 `name`、`status`（ENABLED 恢复停用项目、DISABLED 停用）、`current`（切会话当前项目）、`description`、`isMultiView`；字段缺省表示不改，传 `null` 等于清空。改名、停用、描述、多视图都要求 owner；只带 `id` 是读取，要求 viewer。个人项目（`kind=personal`）owner 不能停用（409），平台管理员仍可经 `/admin/tenants` 停用。**启用与切换已拆开**：`status=ENABLED` 只恢复停用项目（owner，停用项目不在 `Principal.Roles` 里，判权用完整成员关系），不再顺带切当前项目；切换走 `current=true`（要 view 权限）。归档项目要恢复走 `project/restore`，对归档项目发 `status=ENABLED` 由 tenant 服务返回 409。此前两者共用一个信号，导致「点启用」和「侧栏切项目」都会改项目状态。 |
| 20 | `POST /api/dashboard/project/revoke/{id}` | tenants 表 | 已实现 | 软吊销：`disabled=true`，不删除行（Token 审计仍要能追溯）。个人项目 owner 调用返回 409；平台管理员可以。 |
| 扩展 | `POST /api/dashboard/project/delete/{id}` | tenants 表 | 已实现 | §4 清单外的扩展：项目 owner 或平台管理员对**团队项目**做软删除（归档）。`archived_at` 非空并置 `disabled=1`，成员、邀请、API Key 全部保留，复用既有停用路径让主体、项目 Token 与邀请兑换立即失效；重复归档幂等。个人项目返回 409（账号的默认落脚点）。**上游记忆不删**。 |
| 扩展 | `POST /api/dashboard/project/restore/{id}` | tenants 表 | 已实现 | 撤销归档：清空 `archived_at` 并恢复启用，成员与 Key 原样可用。归档项目不在 `Principal.Roles` 里，判权用完整成员关系：平台管理员或项目 owner；未归档返回 409。 |
| 21 | `POST /api/dashboard/profileTemplate/list` | 上游 `/product/list_profile_template` | 已实现 | 项目 viewer。`{list,total,current,size,pages}`，行为 `{...属性树分组,templateId,templateName,projectId,bindingCount,updatedAt}`（分组先铺、身份字段后写，用户数据不能覆盖身份）。 |
| 22 | `GET /api/dashboard/profileTemplate/detail` | 上游 `/product/get_profile_template` | 已实现 | 项目 viewer；`templateId`（或 `id`）必填，`projectId` 缺省取会话当前项目；不存在或不属于该项目都是 404。 |
| 23 | `POST /api/dashboard/profileTemplate/create` | 上游 `/product/add_profile_template` | 已实现 | 项目 editor；`templateName` 必填，其余顶层键（去掉 5 个身份键）整体作为 `metadata`，至少要有一个属性分组（上游 `normalize_template` 拒绝空树，本地先返回 400）；叶子必须是 `{"value":…,"algorithm_updatable":…}` 对象（最多 3 层，形状由上游校验，不合法时上游返回 400 并透传）；返回 `{profile_template_id}`。 |
| 24 | `POST /api/dashboard/profileTemplate/update` | 上游 `/product/update_profile_template` | 已实现 | 项目 editor；`templateId` 必填；仅改名时不带 `metadata`，上游才会保留原属性树；返回 `{profile_template_id}`。 |
| 25 | `POST /api/dashboard/profileTemplate/delete` | 上游 `/product/delete_profile_template` | 已实现 | 项目 editor；上游会把已绑定实例置为 `detached`；返回 `{success:true}`。 |
| 26 | `GET /api/meta` | 控制面配置 + 实时探测 | 已实现 | `{mode:"live",service,keyPrefix,tools,sources,health,authEnabled,authMode}`；登录后才带 `sources`/`health`。`sources.upstream`/`sources.graph` 是真实探测结果（`ok` 或 `unreachable: …`）；`sources.keys`/`sources.callLog` 是全库计数，**只对平台管理员返回**，普通登录用户不返回这两个键。`health` 是上游 `/health` 的原始 JSON（不可达时为 `null`），接口文档页的状态灯直接读它的 `status`。探测有 2s 上限，两个外部探针并行，避免把 `/api/meta` 拖死。 |
| 27 | `GET /api/user/quota/summary` | 控制面 | 已实现 | `{memoryCount:0,knowledgebaseCount:0,knowledgebaseCapacity:0,allowedKnowledgebaseCount:0,enabled:false}`；无配额上游，`enabled:false` 与零值一起表示“未启用”，不伪造。 |


### 为什么有些响应是静态或明确的“未启用”

这些路径的性质不同，不能都归成“没接表”：

  - 明文**只按 id 单把下发**（`keys/copy`），需要项目写权限（editor/owner/平台管理员）；列表始终只有前缀掩码。viewer 拿到 API Key 等于绕过控制台角色直接写入记忆，所以 `canCopy=false`。
  - 历史行（迁移前签发、旧网关导入的 legacy Key）`raw` 为空，接口不下发、显示掩码并提示重新生成；部署配置里的静态 Key 会在下次启动 `Bootstrap` 时补上明文（`UpsertToken` 只在 `excluded.raw <> ''` 时覆盖，不会用空值清掉已有明文）。
  - 前端按 `canCopy` 渲染：真值时在掩码旁给「复制」按钮，点击才调 `keys/copy` 取明文并写入剪贴板（明文不进 DOM、不上屏）；否则 tooltip 里说明「需要编辑权限 / 历史密钥需重新生成」。

判断标准：如果某条路径返回的是“概念不存在”，静态就是正确答案；如果返回的是“数据存在但读不出来”（`keys/copy`），那是设计取舍。两种情况都不应该靠接一张新表来掩盖。

### 知识库 8 条（已接上游）

知识库实体在上游 memos-api，按 `mem_cube_id`（= 授权后的 `projectId`）隔离；控制面只做参数转换与项目级授权，不落库。读要求 `project:view`，写要求 `project:write` 且必须显式带 `projectId`。列表在上游分页之上按名称过滤（最多扫 4000 条），`total` 是过滤后的数量。

| 30 | `POST /api/dashboard/knowledgebase/list` | 上游 `get_knowledgebase` | 已实现：`{records,total,current,size}`，行含 `kbId`/`knowledgebaseName`/`fileCount`/`totalSize`/`createTime`/`updateTime`/`projectId` |
| 31 | `POST /api/dashboard/knowledgebase/detail` | 同上（带 `knowledgebase_id`） | 已实现；不存在或不属于该项目是 404 |
| 32 | `POST /api/dashboard/knowledgebase/file/list` | 上游 `get_knowledgebase_file` | 已实现：`{records,total,current,size}` |
| 33 | `POST /api/dashboard/knowledgebase/memory/list` | — | 503：上游只有知识库与文件管理，没有按知识库召回记忆的接口（`KB_RECALL_ENABLED` 默认关闭），不返回假空列表 |
| 34 | `POST /api/dashboard/knowledgebase/create` | 上游 `create_knowledgebase` | 已实现：`knowledgebaseName` 必填、最长 128 字符 |
| 35 | `POST /api/dashboard/knowledgebase/update` | 上游 `update_knowledgebase` | 已实现：改名/改描述；上游为此新增了端点（见 dumemos `docs/openapi.json`） |
| 36 | `POST /api/dashboard/knowledgebase/project/associate` | — | 409：知识库按项目隔离，没有“多项目共享”这层关系 |
| 37 | `POST /api/dashboard/knowledgebase/project/remove` | — | 409：同上；删除走 `knowledgebase/delete` |

扩展：`knowledgebase/delete`（连同文件删除）、`knowledgebase/file/add`（JSON 形式，`content` 用 data URL/base64/URL 透传）、`knowledgebase/file/delete`、`GET knowledgebase/file/download?...`（唯一不套 JSON 信封的接口，直接回文件流）。

### 清单外扩展（前端有调用、§4 的 29 条未计）

来源：前端调用点 `memory-console/src/pages/MemoryGraph.tsx`（graph 两条）与 `memory-console/src/pages/Models.tsx`（models）。

| 路径 | 数据来源 | 状态 | 来源 |
|------|----------|------|------|
| `GET /api/dashboard/graph/memories` | upstream `POST /product/get_memory_graph` | 已实现：`{nodes,links,stats}`，tenant/types/limit | `MemoryGraph.tsx` |
| `GET /api/dashboard/graph/memory` | upstream `GET /product/get_memory/{id}` | 已实现：按 id 取全文，租户内 | `MemoryGraph.tsx` |
| `GET /api/dashboard/models` | 上游 `/product/models` | 已实现 | 上游从自己进程的环境变量生成清单（`MEMRADER_MODEL`、`MOS_CHAT_MODEL`、`MOS_EMBEDDER_MODEL`、`MOS_RERANKER_MODEL`），控制面转成 `{records:[{role,model,spec,source,group}],total}`。值缺失的行不出现，不返回“规划中”的假模型。 |
| `GET /api/health` | 同进程 MCP `/healthz` + 上游 `/health` | 已实现：`{status,gateway,upstream,checkedAt}` | 接口文档页的状态灯轮询它（30s）。两个探针并发、各 3s 上限；失败原因只给 `timeout`/`unreachable`/`http_<状态码>` 短码，文案由页面翻译。`gateway` 探的是同进程 MCP 监听（`role=all` 时由 `MEMOSD_MCP_ADDR` 推导回环地址）；console 单独部署时为空，会如实报网关不可达。 |

### 本地插件记忆镜像（BFF 扩展）

来源：`docs/module/local-sync.md`（D8）与前端 `LocalMemories.tsx` / `LocalDevices.tsx`。按前端约定用查询串 `projectId`（不是 `tenant`），授权规则是 `auth.InQueryProject`：读要 viewer，删除与转存要 editor。解析后的项目 ID 由 wrapper 写回 `body.projectId`，handler 不读请求里自报的值。

| 路径 | 权限 | 响应要点 |
|------|------|----------|
| `GET /api/dashboard/local-sync/devices?projectId=` | viewer | `{devices,total,quota}`；设备项含行主键 `id` 与插件侧稳定 `deviceId` |
| `DELETE /api/dashboard/local-sync/devices/{id}?projectId=` | editor | `{ok:true}`；按设备吊销，立即生效 |
| `GET /api/dashboard/local-sync/memories?projectId=&layer=&deviceId=&q=&startTime=&endTime=&page=&pageSize=` | viewer | `{records,total,pageNum,pageSize}`；列表不读 `payload` |
| `GET /api/dashboard/local-sync/memories/{id}?projectId=` | viewer | 列表字段 + 解压后的 `payload` JSON 对象 |
| `DELETE /api/dashboard/local-sync/memories/{id}?projectId=` | editor | `{ok:true}`；只软删除控制面副本，不传播到本地 |
| `POST /api/dashboard/local-sync/memories/promote?projectId=` | editor | body `{ids:[...]}` → `{queued:[...],failed:[...]}` |
| `GET /api/dashboard/local-sync/summary?projectId=` | viewer | 分层计数、转存状态、时间范围与 `quota` |

`quota` 在三处一致：`{usedBytes,limitBytes,usedRows,limitRows,pendingBytes,pendingRows}`；`pending*` 是未提交批次占用的字节与 manifest 声明行数，界面必须与 `used*` 并排显示，否则会出现“还有空间却 413”。`startTime`/`endTime` 作用于 `source_updated_at`，接受 RFC3339 或 `YYYY-MM-DD`，`startTime > endTime` 返回 400。

`MEMOSD_LOCAL_SYNC_ENABLED=false`（默认）时这 7 条统一返回 503 `code=50300`，不返回空列表；能力关闭属于部署状态，不是“没有数据”。

### 响应形状（要点）

- 项目：`{id,name,description,status,createTime,isMultiView,kind,archived[,archivedAt],disabled,role,member}`；`createTime` 是 ISO（来自 `created_at`），`archivedAt` 是北京时间。
- 密钥：`{id,keyName,apiKey,canCopy,createTime,expireTime[,lastUsedAt]}`；列表里的 `apiKey` 是前缀掩码，`createTime`/`expireTime`/`lastUsedAt` 是北京时间 `YYYY-MM-DD HH:mm:ss`，永不过期的 `expireTime` 为空串，从未使用不带 `lastUsedAt`。创建响应把 `apiKey` 换成明文并带 `canCopy=true`。
- 记忆详情：`{memoryId,memoryContent,subjectType,externalUserId,createTime,...}`。
- 模型：`{role,model,spec,source,group}`。

## 错误与边界情况

- 上游不可用：502/503，与 rest 的映射一致。
- 只有变更类路由（项目 create/update/revoke、密钥 create/revoke、记忆 delete、prompt save/reset、画像模板 create/update/delete、知识库写）写审计，`Tool` 为 `dashboard.<动作>`；读接口（list/detail/dict/query/generate/debug/models/graph/meta）不写，避免自动刷新把审计表刷满。
- 分页参数非法（0、负数、超大）按默认值夹取，不报错。

## 测试与验证

- `httptest`：已实现路由各一条成功路径 + 401/400/404 边界；Graph 为 nil 时的 503；prompt 五条（默认值/保存/重置/生成/干跑）与 models 清单；密钥列表按写权限下发明文或掩码（`TestKeysCreateListAndCopy`、`TestKeyJSONFallsBackToMask`）；`keys/create` 带/不带/0 天 `expireDays` 的落库与 `expireTime`，以及负数、超上限、小数、非数字、布尔的 400。
- golden 钉住全部已实现路由的状态与信封形状，含 prompt 五条、models、`/api/health` 与本地同步 7 条；知识库写四条与 `detail` 钉住 503 与 `code=50300`。
- `TestOpenAPIDashboardContract` 把 `routes` 记录的路由表与 `api/openapi.dashboard.yaml` 双向对齐：实现了没写进 spec、spec 写了没实现，都失败。
- 第 3 层验证覆盖 `/healthz`、`/readyz`、`/api/meta` 与 `/api/health`。

## 参考来源

- `memory-console/docs/BACKEND-INTEGRATION.md` §4（逐条语义、坑与规模数据）。
- `memory-console/src/lib/api.ts`（信封解包：`code===0` 取 `data`）。
- `api/openapi.dashboard.yaml`（48 条声明，控制台契约的唯一真源；2026-10-28 从前端仓库的 `openapi.json` 迁入并改为 YAML 3.0.3）。

## 实现记录

See the public version-control history.
## 变更记录

- 2026-10-24（软删除与知识库接线）：项目删除改为归档（`archived_at` + `disabled`），新增 `project/restore/{id}`；项目列表带 `archived`/`archivedAt`，`dict` 与 `/api/auth/me` 不返回归档项目；owner 通过完整成员关系判权，平台管理员看全部。知识库 8 条从占位改为代理上游，新增 create/update/delete/file add/file delete/file download 与 `keys/rename`。上游 dumemos 新增 `POST /product/update_knowledgebase`。验证：`go test ./...`、`make lint`、`node scripts/verify-i18n.mjs`、前端 `tsc --noEmit`。
- 2026-10-26（租户规则收口）：`project/*` 写路由不再直接持有仓储，创建、改名、停用、归档、恢复的状态规则（命名、`u-` 保留前缀、个人项目不能停用/删除、归档项目只能先恢复）改由 `service/tenant` 判定，`Deps.InvalidateAuth` 随之移除。对外行为不变，仅有错误优先级调整（先判权再判状态）：无权限的调用者恢复项目得到 403（原先是 409「项目未归档」，恢复不存在的项目原先是 404）；只有 view 权限的调用者对归档项目发 `status=ENABLED` 得到 403（原先是 409）。有权限的调用者结果不变。验证：`go test -race ./internal/transport/dashboard ./internal/app`，golden 无需改动。

- 2026-10-26（停用可逆与个人空间成员）：`project/list` 对停用项目用完整成员关系回填真实 `role` 并继续返回（`dict` 仍不返回，避免切到不可用项目），`project/update status=ENABLED` 的纯启用分支改用 `MemberRoles` 判 owner，恢复后返回的 `role/member` 也从完整成员关系取；此前停用后 owner 的 `Roles` 为空，项目从列表消失且无法自行启用，停用实际不可逆（`689c20b` 拆启停时只修了管理员路径）。个人空间过滤从「非本人一律隐藏」改为「非本人且无真实成员关系才隐藏」，让被邀请/加入的成员能看到。`project/create` 的 `role/member` 改为描述调用主体：账号创建者为 owner，管理 Token 不是成员，不再无条件写 `owner/true`。`/api/meta` 的 `keys`/`callLog` 全局计数只对平台管理员返回。验证：`TestE2EDisabledProjectOwnerCanReEnable`、`TestE2EPersonalSpaceMemberVisible`、`TestE2EProjectCreateReportsRealMembership`、`TestE2EMetaGlobalCountsOnlyForPlatformAdmin`；`make check` 全绿。提交：见本次提交。
- 2026-10-29（413 信封码）：`mapError` 对 `httpx.ErrBodyTooLarge` 与 `domain.ErrTooLarge` 统一返回 HTTP 413 + `41300`（原先是 413 + `40000`）。这是**全局行为变化**：任何 dashboard 路由超过请求体上限（`MEMOSD_MAX_BODY_BYTES`，默认 4 MiB）都用 `41300`，不再混在参数错误里。既有前端已按 413 状态码与 `41300` 双判断，无需改动。验证：`TestDashboardBodyLimitUsesPayloadTooLargeCode`（`project/list` 超过上限 -> 413 + 41300）。
- 2026-10-24（本地插件记忆镜像）：新增 7 条 `/api/dashboard/local-sync/*`（设备列表/吊销、镜像列表/详情/删除、批量转存、治理摘要），授权用新增的 `auth.InQueryProject`（查询串 `projectId`，读 viewer、写 editor）。能力默认关闭（`MEMOSD_LOCAL_SYNC_ENABLED=false`，插件尚未发版），关闭时统一 503；golden 增加这 7 条的已开启形状，fake 服务注入固定的设备与镜像数据。验证：`go test ./internal/transport/dashboard`、`make check`。

### 聊天记忆活动（本任务补充）

`POST /api/dashboard/project/memory/activity`，body `{projectId,callIds?,after?}`（callIds最多20个）。
要求项目查看权限，按租户和call_id精确查审计，返回`records`：
`{callId,status,completedAt?,memories}`，status为pending/succeeded/failed/unavailable/not_found。
memories与记忆详情同形，真实记忆详情补充memoryKey/memoryType/status/tags/updateTime。
不传callIds时按项目读取最近20条已完成日志，after为RFC3339时间游标，响应cursor用于增量读取；不依赖聊天历史是否仍保留工具回执。
全部记忆读取再次校验项目归属；不返回审计入参、密钥、其他项目或不可证明归属的记忆。
记忆详情请求显式要求projectId，与现有实现的权限口径一致。

## 记忆详情侧栏（2026-10-31，implemented）

已有memory/detail响应增加version/history/sources供基本信息、来源与版本页签使用；读取仍要求projectId与记忆归属。原事件详情补充相同元信息。当前版本号来自上游；没有历史时只显示当前内容与无历史提示，不生成假时间线。聊天评分单独走chat路由，不混用记忆纠错。
