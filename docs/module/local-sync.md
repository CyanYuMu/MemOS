---
module: local-sync
packages:
  - internal/service/localsync
  - internal/transport/localsync
status: implemented
---

# local-sync

接收本地插件上传的 L1 Trace / L2 Policy / L3 World Model / Skill 副本，在控制面保存为**只读镜像**，供控制台展示、审计与逐条转存；本地存储始终是权威源。设计决议 D1–D10 见本文末尾的《设计决议》，本文是落地契约。

status 含义：`draft` 还在写，不得写代码；`accepted` 契约已定，可以开始实现；`implemented` 代码与测试已落地并与本文一致。

## 职责与边界

- 负责：
  - 设备授权码流程（OAuth Device Flow 风格）与设备令牌的签发、滑动过期、按设备吊销。
  - 分块上传、行级幂等、内容去重、部分失败回报、项目配额、请求限流。
  - 本地记忆镜像的存储（控制面 SQLite）、只读查询与软删除。
  - 逐条/批量“转存为云端记忆”：经既有 `jobs` 队列调用上游 `add_message`，成功后回写转存结果。
  - 授权、吊销、提交、拒绝、删除、转存六类审计事件。
- 不负责：
  - 检索 / RAG / 抽取 / Dream：仍在数据面 memos-api；控制面不把镜像喂进上游抽取管线。
  - 云端 → 本地反向同步；不提供墓碑，也不传播本地删除。
  - 插件内的 bundle 构建、层开关、`share_scope` 过滤与“默认不上传”的产品默认值：由插件负责，控制面只按请求声明做服务端校验。
  - MCP 的 5 个工具契约：本模块不改，也不复用其路由。

**边界决议（D2）**：控制面原本只存租户、凭据、审计与任务元数据，不持有记忆正文。本模块显式扩展为“可以持有客户端上传来源的记忆副本”，但只用于展示、审计与显式转存，**不承担检索**，不与上游 memos-api 的抽取管线合并。

## 依赖

- 依赖 `internal/domain`（`LocalDevice`、`LocalMemory`、`LocalLayer`、`CapLocalSync` 等）与 `internal/platform/logging`。
- 服务以使用方定义的窄接口依赖存储；adapter 不 import service（depguard 强制）：

```go
// internal/service/localsync
type Store interface {
    CreateDeviceCode(ctx context.Context, c domain.LocalDeviceCode) error
    DeviceCodeByHash(ctx context.Context, hash [32]byte) (domain.LocalDeviceCode, error)
    ApproveDeviceCode(ctx context.Context, hash [32]byte, accountID, tenantID, label string, at time.Time) error
    DenyDeviceCode(ctx context.Context, hash [32]byte, at time.Time) error
    ConsumeDeviceCode(ctx context.Context, hash [32]byte, dev domain.LocalDevice, at time.Time) error
    DeviceByTokenHash(ctx context.Context, hash [32]byte) (domain.LocalDevice, error)
    TouchDevice(ctx context.Context, id string, at time.Time) error
    RevokeDevice(ctx context.Context, tenantID, deviceID string, at time.Time) error
    ListDevices(ctx context.Context, tenantID string) ([]domain.LocalDevice, error)

    GetMembership(ctx context.Context, accountID, tenantID string) (domain.Membership, error)
    GetAccount(ctx context.Context, id string) (domain.Account, error)
    GetTenant(ctx context.Context, id string) (domain.Tenant, error)

    CreateBatch(ctx context.Context, b domain.LocalSyncBatch) error
    GetBatch(ctx context.Context, tenantID, deviceID, batchID string) (domain.LocalSyncBatch, error)
    PutChunk(ctx context.Context, tenantID, deviceID, batchID string, seq int,
        body []byte, maxBatchBytes int64, at time.Time) (received int64, err error)
    ListChunkSeqs(ctx context.Context, tenantID, deviceID, batchID string) ([]int, error)
    GetChunk(ctx context.Context, tenantID, deviceID, batchID string, seq int) ([]byte, error)
    FinishBatch(ctx context.Context, tenantID, deviceID, batchID string,
        result domain.LocalCommitResult, at time.Time) error
    PurgeBatchesBefore(ctx context.Context, before time.Time) (int64, error)

    ApplyRows(ctx context.Context, tenantID, deviceID string, rows []domain.LocalRow,
        limits domain.LocalQuota) (applied, unchanged int64, err error)
    LocalState(ctx context.Context, tenantID, deviceID string) (domain.LocalState, error)
    Quota(ctx context.Context, tenantID string) (domain.LocalQuota, error)

    ListMemories(ctx context.Context, f domain.LocalMemoryFilter) ([]domain.LocalMemory, int, error)
    GetMemory(ctx context.Context, tenantID, id string) (domain.LocalMemory, error)
    DeleteMemory(ctx context.Context, tenantID, id string, at time.Time) error
    MarkPromoteQueued(ctx context.Context, tenantID, id, jobID string, at time.Time) error
    MarkPromoteResult(ctx context.Context, tenantID, id, status, jobID, message string, at time.Time) error
    EnqueueJob(ctx context.Context, j domain.Job) error
}

// Adder 由 service/memory 结构化满足；转存作业同步调用上游 add_message。
type Adder interface {
    Add(ctx context.Context, caller domain.Caller, req domain.AddRequest) (domain.AddResult, error)
}

type Auditor interface {
    Record(ctx context.Context, rec domain.AuditRecord)
    RecordNow(ctx context.Context, rec domain.AuditRecord) error
}
```

- 被 `internal/app` 装配；`internal/transport/localsync`（`/api/local-sync/*` 与 `/__device`）与 `internal/transport/dashboard`（`/api/dashboard/local-sync/*`）依赖服务。`internal/worker` 通过可选接口调用转存作业。
- 存储实现在 `internal/adapter/store/local_sync.go`，迁移 `migrations/0035_local_sync.sql`。

## 契约

### 设备授权码与设备令牌（D4）

| 方法 | 路径 | 凭据 | 说明 |
|------|------|------|------|
| POST | `/api/local-sync/device/code` | 无 | `{deviceLabel?,agentKind?}` → 200 `{deviceCode,userCode,verificationUri,expiresIn,interval}` |
| POST | `/api/local-sync/device/token` | 无 | `{deviceCode}` → 202 `{code:20200,data:{status:"authorization_pending"}}`；批准后 200 `{deviceToken,deviceId,tenantId,projectName}` |
| GET | `/__device?code=<userCode>` | 账号会话 | 浏览器确认页（项目、设备名、授权范围） |
| POST | `/__device` | 账号会话 | 表单或 JSON `{userCode,tenantId,label,allow}`；`allow=false` 表示拒绝 |

- `verificationUri` 由配置的 `MEMOSD_LOCAL_SYNC_VERIFICATION_URI` 或请求 Host 推导，末尾自动带 `?code=<userCode>`，用户点开即进入确认页。

- **授权页 POST 的同源校验**：只接受 `Sec-Fetch-Site` 为 `same-origin`/`none`/缺省，且 `Origin`（存在时）与 `Host`（或网关写入的 `X-Forwarded-Host`）同源。Chrome 对同源 HTML 表单提交（导航型 POST）会发 `Origin: null`，因此 `null` 按“没有 Origin”处理；跨站表单会带真实来源或 `Sec-Fetch-Site: cross-site`，仍被拒绝。与 [invites](invites.md) 的邀请兑换同口径。

- `deviceCode` 是 32 字节随机值的 hex；库里只存 SHA-256。`userCode` 是 8 位、去掉易混字符的用户可读码（`XXXX-XXXX`），唯一，有效期默认 10 分钟。
- 授权页只列出调用者在其中**至少 editor** 的项目；项目列表与批准都按成员表判权（不读 `Principal.Roles`，平台管理员的该映射按设计为空），批准时服务端再次校验 `CapLocalSync`（等价 `role >= editor`），不信任页面自报。
- 设备令牌格式 `memosls_<64 hex>`（32 字节随机）；库里只存 SHA-256，登录时先做固定前缀快速判定再按哈希查找。令牌只对 `/api/local-sync/*` 生效，不能用于 MCP、REST、Dashboard 或 Admin。
- 有效期是**滑动 180 天**：`last_seen_at` 每次请求（节流 1 分钟）刷新；`COALESCE(last_seen_at, created_at) + 180 天` 早于当前时间即 401。按设备单独吊销（`revoked_at`），吊销立即生效。
- **每次请求复核**：设备所属账号必须存在且未禁用、仍是该设备绑定项目的成员、角色 `>= editor`；任一不满足时账号/设备状态问题返回 401，成员关系或角色不足返回 403。项目被停用同样 403。
- 一个设备令牌只绑定一个项目；换项目必须重新授权。授权码只能用一次：轮询换取令牌时把状态从 `approved` 条件更新为 `consumed`，并发轮询只有一个成功。

### 上传协议（D5、D6）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/local-sync/state` | `{deviceId,tenantId,lastBatchId,lastAcceptedAt,quota:{usedBytes,limitBytes,usedRows,limitRows,pendingBytes,pendingRows}}` |
| POST | `/api/local-sync/batches` | `{batchId,layers,manifest:{layer:count},traceMode:"summary"\|"full"}` → `{batchId,expiresAt}` |
| PUT | `/api/local-sync/batches/{batchId}/chunks/{seq}` | body 是 `application/gzip` 的 JSONL → `{received}` |
| POST | `/api/local-sync/batches/{batchId}/commit` | → `{status,accepted,rejected,rejectedReasons:[{layer,id,reason}],cursor}` |
| POST | `/api/local-sync/manual` | 仅账号会话；bundle JSON → 同 commit 结果 |

JSONL 每行：

```json
{"layer":"trace|policy|world_model|skill","id":"...","contentHash":"sha256:...","contentDigest":"sha256:...","updatedAt":1700000000000,"title":"...","summary":"...","payload":{}}
```

不变量：

- `batchId` 由客户端生成，`[A-Za-z0-9._-]{8,128}`，**全局唯一**（表主键）；同一 `(tenant_id, device_id)` 重复创建返回 409，跨项目复用同一 ID 也会 409。插件用 UUID，实际不会撞。`seq` 为 `0..1023` 的整数，同一 `seq` 重发覆盖，`received` 是批次累计字节。
- 单块 body ≤ 4 MiB（gzip 压缩后），单批累计 ≤ 64 MiB；未提交批次 TTL 默认 24 小时，超期逻辑上不可续传，并在下一次创建批次时机会式清理。
- **提交后立即删除分块**：分块是客户端上传的原始 JSONL，summary 模式下被拒的原始正文也保存在里面；批次 committed 后在同一个事务里 `DELETE FROM local_sync_chunks`，只保留计数与拒因。未提交批次仍按 TTL 级联清理。
- **commit 幂等**：首次 commit 在事务外完成行处理后把批次置为 `committed` 并保存结果 JSON；相同批次再次 commit 直接返回首次结果，不重复写入。未处理完的批次保持 `pending`，重放会按行级幂等键安全重做。
- 行级去重键 `UNIQUE(tenant_id, device_id, layer, source_id)` + `content_hash`：内容未变则跳过（计入 `accepted`，不重写、不刷新 `synced_at`）；内容变化则更新并以新的 `source_updated_at` 作为游标。`source_updated_at` 来自客户端 `updatedAt`（epoch 毫秒，必须为正），服务端不采信客户端时钟做授权判断。
- `contentDigest` 可选，给了必须是 `sha256:<64 hex>`，否则整行拒绝。它是**去掉 `updatedAt`** 的规范 JSON 哈希：插件因 embedding 重试只刷新时间戳时 `contentHash` 会变而 `contentDigest` 不变。两者都存在时服务端优先按 digest 判断内容是否相同——内容相同但时间戳变了，不重写正文，只推进 `source_updated_at`（`/state` 游标取自该列，不推进会让客户端每轮重发同一行）；墓碑匹配同样优先用 digest，避免“时间戳变一行、内容没变”被当成新内容复活已删除的镜像。旧客户端不带该字段时退回 `contentHash` 比较。
- **单行失败不整批失败**：行格式不合法、`layer` 未在批次里声明、超过单行 256 KiB、manifest 数量之外的额前行、summary 模式出现原始正文，都只拒绝该行并写入 `rejectedReasons`；整批仍返回 200 与 `status:"committed"`。数据库/存储错误才让请求失败。
- **配额**：单项目 2 GiB / 50 万行（含软删除行不计入配额，删除即释放额度）。分块写入的准入检查复用 10 秒的已用量缓存，避免每个分块都全表 `SUM(LENGTH(payload))`；存活行变化（提交、删除、清理）立即失效缓存，最终精确裁决仍在 `ApplyRows` 事务内。超出返回 413 `code=41300`，整批不落库。配额按项目统计，不按设备。实现上 commit 先解析全部分块，再用**一次** `ApplyRows` 事务写入；任何分块触配额都会整体回滚，不会出现“前面的块已落库、后面的块失败”的半批状态。
- **未提交分块也占配额**：`POST /batches` 与 `PUT /chunks/{seq}` 在写入前后都会把项目内待提交批次的分块压缩字节计入 2 GiB（`pending` 与 `used` 相加），新建批次还会把 manifest 声明的行数计入 50 万行；超出立即 413，避免 24h TTL 内反复创建批次把共享库写满。提交或过期回收后 `pending` 归零。`quota.pendingBytes`/`pendingRows` 就是这部分占用：**它们单独给，不并入 `usedBytes`/`usedRows`**，客户端判断剩余空间时按 `used + pending` 与上限比较。
- **整批解压上限**：单块解压上限 32 MiB 之外，一次 commit 的全部块解压总量不得超过 256 MiB（`maxBatchDecompressed`），否则 400 拒绝且不落库；这是对“1024 个高压缩比小块”内存放大的硬边界。
- **限流**：设备维度的令牌桶（默认 120 次/分钟）作用于创建批次、上传分块与 commit；超出返回 429 `code=42900` 并带 `Retry-After`。手工上传受 HTTP 请求体上限（`MEMOSD_MAX_BODY_BYTES`，默认 4 MiB）约束。
- 游标 `lastAcceptedAt` 是该设备已被接受的最大 `source_updated_at`，插件只发更新的行；不做服务端强制过滤，旧行仍按内容哈希幂等处理。`/state` 与 commit 的游标时间用 **RFC3339Nano**（保留毫秒），与客户端 `updatedAt` 的毫秒精度对齐；列表类接口展示时间仍用 `RFC3339`。
- **重复 batchId 先判重**：`POST /batches` 在配额检查之前先查 `(tenant, device, batchId)`，已存在直接 409，避免响应丢失后的幂等重试在接近配额时被误报 413。manifest 单层声明不得超过项目行数上限（50 万），防止求和溢出绕过行配额预占。
- **删除单向**：本地删除不会传播到云端；控制台删除只把镜像行置 `deleted_at`，不触及本地。云删除后同一内容再次上传视为“内容未变”继续跳过；内容变化才重新出现。软删除行被 `MEMOSD_LOCAL_SYNC_DELETED_RETENTION` 物理清理后，控制面仍保留一条**轻量墓碑**（`local_memory_tombstones`，只存 tenant/layer/source_id/content_hash/content_digest/deleted_at），所以解绑重绑、从 0 全量重扫或 `/state` 游标下调都不会让已删除的内容复活。镜像保留期清理（`MEMOSD_LOCAL_SYNC_MIRROR_RETENTION`）删除**已软删除**的行时同样先写墓碑；仅因超期被清掉的存活行不写墓碑，重新同步回来是正确的（它们没有被用户删除）。墓碑默认保留 180 天（`MEMOSD_LOCAL_SYNC_TOMBSTONE_RETENTION`），从写入墓碑的时刻起算，到期由同一维护循环清理。迁移 0031 之前的墓碑没有 `content_digest`，只能按 `content_hash` 判定；旧客户端不带该字段时同样退回哈希比较，仅时间戳刷新无法与内容变化区分。这一语义必须在用户可见文档与页面提示中写明。
- `traceMode=summary` 时，`layer=trace` 行的 `payload` **只允许** `summary`、`tags`、`toolNames`、`toolCount`、`ts` 五个键；出现任何其它键（尤其是原始 prompt / 工具 IO 字段）整行拒绝。`full` 模式不做该限制，但服务端不承诺脱敏。
- **脱敏在客户端完成**：插件提供上传前敏感信息预检（`off`/`warn`/`redact`，默认 `warn`），`redact` 会把命中值替换为 `[REDACTED:<kind>]` 并重算幂等键。控制面把这些占位符当作普通内容存储，不重新识别、不还原，也不因为 payload 含占位符而区别对待；因此从 `redact` 切到 `warn`/`off` 后，**内容有变化**的行再次上传会按原文覆盖镜像。服务端自己的边界仍是“上游 memos-api 不接收原始 trace”和 `summary` 模式的字段白名单。
- 行里的 `layer` 必须属于创建批次时声明的 `layers`；`manifest` 是每层的声明行数，服务端按行计数，只接受每层前 `count` 行，多余行逐行拒绝。
- 手工上传落合成设备 `manual`（不是真实设备、不出现在设备列表），复用同一套校验、幂等、配额与审计；请求必须带 `projectId`，调用者在该项目至少 editor。手工上传只要求账号主体（`PrincipalAccount`），**不要求存在本地会话**：零信任网关注入的外部身份同样没有 `SessionID`；管理 Token 返回 400。

### 存储（D7 与落地补充）

```sql
local_device_codes(
  code_hash BLOB PRIMARY KEY, user_code TEXT NOT NULL UNIQUE,
  device_label TEXT NOT NULL DEFAULT '', agent_kind TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,            -- pending | approved | denied | consumed
  account_id TEXT NOT NULL DEFAULT '', tenant_id TEXT NOT NULL DEFAULT '',
  expires_at TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT)

local_devices(
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
  device_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', agent_kind TEXT NOT NULL DEFAULT '',
  profile_id TEXT NOT NULL DEFAULT '', token_hash BLOB NOT NULL UNIQUE,
  status TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT, revoked_at TEXT)
CREATE UNIQUE INDEX ... ON local_devices(tenant_id, device_id);

local_sync_batches(
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, device_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- pending | committed
  layers TEXT NOT NULL, manifest_json TEXT NOT NULL DEFAULT '{}',
  manifest_hash TEXT NOT NULL DEFAULT '',
  trace_mode TEXT NOT NULL DEFAULT 'full',
  received_bytes INTEGER NOT NULL DEFAULT 0,
  accepted INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '',
  cursor_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, committed_at TEXT)
CREATE UNIQUE INDEX ... ON local_sync_batches(tenant_id, device_id, id);

local_sync_chunks(
  batch_id TEXT NOT NULL REFERENCES local_sync_batches(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, body BLOB NOT NULL, received_bytes INTEGER NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (batch_id, seq));

local_memories(
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, device_id TEXT NOT NULL,
  layer TEXT NOT NULL, source_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  payload BLOB NOT NULL,           -- gzip 后的原始 JSON
  source_updated_at TEXT NOT NULL, deleted_at TEXT,
  promoted_memory_id TEXT NOT NULL DEFAULT '', promote_job_id TEXT NOT NULL DEFAULT '',
  promote_status TEXT NOT NULL DEFAULT '', promote_error TEXT NOT NULL DEFAULT '',
  synced_at TEXT NOT NULL)
CREATE UNIQUE INDEX ... ON local_memories(tenant_id, device_id, layer, source_id);
CREATE INDEX ... ON local_memories(tenant_id, layer, source_updated_at DESC);
CREATE INDEX ... ON local_memories(tenant_id, device_id);
```

与 design.md D7 的差异（均为落地必需，已在实现记录说明）：

- 新增 `local_device_codes` 保存短时授权码。把授权码塞进 `local_devices` 会产生大量“尚未授权”的伪设备行，且授权码与设备令牌的生命周期完全不同。
- 新增 `local_sync_chunks` 保存分块。D7 只描述批次元数据，分块正文没有落点；放在独立表可按 `seq` 覆盖并使用外键级联清理。
- `local_sync_batches` 增加 `manifest_json`（commit 需要每层声明行数；`manifest_hash` 只用于快速比对）、`trace_mode`（commit 时校验 summary）、`result_json`/`cursor_at`（幂等重放返回首次结果）。`trace_mode` 的列默认值是 `full`，但服务层在请求未声明时按 `summary` 归一，绝不因缺省而上传原始 trace。
- `local_memories` 增加 `promote_status`/`promote_job_id`/`promote_error`，用于“已入队 / 成功 / 失败可重试”的状态机。
- `local_memories.payload` 存 gzip BLOB；列表查询的 `SELECT` 不含 `payload`，详情才解压。
- `local_memory_tombstones`（迁移 0030）按 `(tenant_id, layer, source_id)` 主键保存内容哈希与删除时间。软删除清理时先写墓碑再删行；`ApplyRows` 遇到同哈希重传直接按“内容未变”跳过，哈希不同则删墓碑并允许作为新行出现。墓碑很小且每键只有一行，不随删除次数增长。

### 控制台查询（D8）

| 方法 | 路径 | 授权 | 说明 |
|------|------|------|------|
| GET | `/api/dashboard/local-sync/devices?projectId=` | viewer | `{devices,total,quota}`；设备项 `{id,deviceId,accountId,label,agentKind,status,createdAt,lastSeenAt,revokedAt}` |
| DELETE | `/api/dashboard/local-sync/devices/{id}?projectId=` | editor | `{id}` 是设备行主键；同时兼容传 `deviceId` |
| GET | `/api/dashboard/local-sync/memories?projectId=&layer=&deviceId=&q=&startTime=&endTime=&page=&pageSize=` | viewer | `{records,total,pageNum,pageSize}` |
| GET | `/api/dashboard/local-sync/memories/{id}?projectId=` | viewer | 摘要 + `contentHash` + 解压后的 `payload` JSON 对象 |
| DELETE | `/api/dashboard/local-sync/memories/{id}?projectId=` | editor | 软删除云端副本 |
| POST | `/api/dashboard/local-sync/memories/promote?projectId=` | editor | `{ids:[...]}` → `{queued,failed}` |
| GET | `/api/dashboard/local-sync/summary?projectId=` | viewer | 治理视图：分层计数、转存状态、时间范围与配额 |

- `startTime`/`endTime` 作用于 `source_updated_at`，接受 RFC3339（如 `2026-10-01T00:00:00Z`）或 `YYYY-MM-DD`；省略表示不设边界，`startTime > endTime` 返回 400。列表返回的 `sourceUpdatedAt`/`syncedAt` 都是 RFC3339。
- 列表项 `{id,deviceId,deviceLabel,layer,sourceId,contentHash,title,summary,sourceUpdatedAt,syncedAt,promoted,promotedMemoryId,promoteStatus,promoteError}`；列表不读 payload。`deviceLabel` 来自设备表，手工上传的行没有设备，显示为空串。
- 设备项同时给出 `id`（行主键，删除用）与 `deviceId`（插件侧稳定 ID，记忆过滤用）；`status` 只会是 `active` 或 `revoked`，时间字段无值时为空串。
- `payload` 在详情里直接返回解析后的 JSON 对象（不是 gzip、不是字符串），前端按层次化渲染；只有项目 viewer 及以上可读。
- `summary` 返回 `{total,deleted,promoted,promotePending,promoteFailed,layers:{layer:count},oldestSyncedAt,newestSyncedAt,quota}`；`layers`/`total` 只计未删除行，`deleted` 单独给软删除数；时间字段为空时是空串。
- 三处返回的 `quota` 字段一致：`{usedBytes,limitBytes,usedRows,limitRows,pendingBytes,pendingRows}`。`used*` 是已落库镜像，`pending*` 是尚未提交批次（分块字节 + manifest 声明行数）。准入校验用两者之和，界面展示也必须并排显示，否则会出现“还有空间却 413”。

### 转存（D1）

- `POST /api/dashboard/local-sync/memories/promote?projectId=` body `{ids:[...]}` → `{queued:[...],failed:[{id,message}]}`。
- 对每条镜像校验：属于该项目、未删除、至少有 title/summary/payload 之一；已 `queued/running/succeeded` 的行直接计入 `queued`（幂等，不重复入队）；`failed` 的行允许重试。
- 入队 `jobs.kind=local_promote`，payload 含本地记忆 ID、项目、上游 user_id 与构造好的消息。worker 复用既有退避、并发与死信语义，调用 `service/memory.Add`（同步）写上游。
- **`promoted_memory_id` 的取值**：上游 `POST /product/add` 不返回记忆 ID（只返回 `{status,message,usage}`），因此成功转存时该列保持为空；作业本身由 `promote_job_id` 追溯，`promoted` 字段以 `promote_status=succeeded` 为准。若上游未来返回真实 ID，只需在回写点填入。重复转存靠 `promote_status` 与行级幂等收敛，不依赖该值。
- 转存正文由 `title + summary + payload(JSON)` 组成一条 `user` 消息；`traceMode=full` 的行会包含原始内容，这是用户显式操作，服务端不自动转存。

### 审计事件

复用 `audit_records`，新增 `Tool`：

| Tool | 触发点 | Input 摘要（不含凭据与正文） |
|------|--------|------------------------------|
| `local_sync.device.code` | 插件请求授权码 | `device_label`、`agent_kind` |
| `local_sync.device.authorize` | 用户批准/拒绝 | `device_label`、`tenant_id`、`allow` |
| `local_sync.device.revoke` | 控制台吊销 | `device_id`、`tenant_id` |
| `local_sync.batch.commit` | commit 成功（含部分拒绝） | `batch_id`、`accepted`、`rejected`、`layers`、`trace_mode` |
| `local_sync.batch.reject` | commit 中有被拒行 | 同上的计数与按层拒因统计，不含行内容 |
| `local_sync.manual.commit` | 手工上传 | `batch_id`、`tenant_id`、计数 |
| `local_sync.memory.delete` | 控制台删除镜像 | `memory_id`、`tenant_id` |
| `local_sync.memory.promote` | 转存入队 | `memory_ids`、`tenant_id` |
| `local_sync.memory.promote.background` | 转存作业终态 | `memory_id`、`job_id`、成功/失败 |
| `local_sync.memory.retention` | 镜像保留期清理 | `purged`、`before`、`days`（不含正文） |

设备令牌只在换取成功的响应里出现一次；授权码明文只出现在授权流程的响应与授权页里。设备令牌、授权码、payload 正文都不进审计与日志；payload 只在有项目读权限的详情接口里返回。

### HTTP 契约与错误码

- 信封沿用 `{code,data,message}`；成功 `code=0`，pending 轮询 `code=20200`。
- 新增 `41300`（配额超限）。Dashboard 与 `/api/local-sync/*` 的“请求体过大”都保持 `40000`（413），只有 `domain.ErrQuotaExceeded` 映射到 `41300`，避免新增能力改动 29 条老路由的契约。其余复用：`40000` 参数、`40100` 未授权、`40300` 无权限、`40001` 不存在、`40900` 冲突、`42900` 限流、`50300` 能力未启用/上游不可用、`50000` 内部错误。
- 能力未开启（`MEMOSD_LOCAL_SYNC_ENABLED=false`）时所有 `/api/local-sync/*` 与 Dashboard 的本地同步读写返回 503 `50300`，不返回假空数据；Dashboard 列表也不伪造 `enabled:true`。

### 配置

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `MEMOSD_LOCAL_SYNC_ENABLED` | `false` | 默认关闭：插件端尚未发版，打开后才暴露本模块接口。关闭时所有 `/api/local-sync/*` 与 Dashboard 本地同步路由返回 503 |
| `MEMOSD_LOCAL_SYNC_VERIFICATION_URI` | 空 | 授权页绝对地址；空时按请求 Host 推导 `/__device` |
| `MEMOSD_LOCAL_SYNC_DEVICE_CODE_TTL` | `10m` | 授权码有效期 |
| `MEMOSD_LOCAL_SYNC_POLL_INTERVAL` | `5s` | 建议轮询间隔 |
| `MEMOSD_LOCAL_SYNC_TOKEN_TTL` | `4320h`（180 天） | 设备令牌滑动过期窗口 |
| `MEMOSD_LOCAL_SYNC_BATCH_TTL` | `24h` | 未提交批次保留时间 |
| `MEMOSD_LOCAL_SYNC_MAX_CHUNK_BYTES` | `4MiB` | 单块压缩后上限 |
| `MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES` | `64MiB` | 单批压缩后上限 |
| `MEMOSD_LOCAL_SYNC_MAX_ROW_BYTES` | `256KiB` | 单行 payload 序列化后上限 |
| `MEMOSD_LOCAL_SYNC_MAX_BATCH_DECOMPRESSED` | `256MiB` | 单次 commit 全部块解压后的总量上限；显式设置时不得小于 `MAX_BATCH_BYTES`，未设置时自动跟随（取两者较大值，兼容存量部署调大单批上限） |
| `MEMOSD_LOCAL_SYNC_QUOTA_BYTES` | `2GiB` | 单项目镜像配额（含未提交分块） |
| `MEMOSD_LOCAL_SYNC_QUOTA_ROWS` | `500000` | 单项目行数配额 |
| `MEMOSD_LOCAL_SYNC_RATE_PER_MINUTE` | `120` | 设备维度写请求限额 |
| `MEMOSD_LOCAL_SYNC_DELETED_RETENTION` | `168h`（7 天） | 软删除行的物理清理期限；`0` 表示不清理 |
| `MEMOSD_LOCAL_SYNC_MIRROR_RETENTION` | `0` | 镜像保留期；`0` 表示永久保留。到期只删控制面副本 |
| `MEMOSD_LOCAL_SYNC_TOMBSTONE_RETENTION` | `4320h`（180 天） | 删除墓碑的保留期；`0` 表示永不清理。从墓碑创建（物理清理）时刻起算 |

### 治理：镜像保留期

- 默认 `0`，副本永久保留；配置正数（如 `720h`）后，worker 的小时级清理任务会删除
  `synced_at` 早于 `now - 保留期` 的镜像行（含已软删除行），并写审计
  `local_sync.memory.retention`（只含条数与截止时间）。
- 只影响控制面副本：本地插件数据与已转存到云端的记忆都不受影响。转存后的行即使
  镜像被清理，云端记忆仍在；再次上传会重新生成镜像。
- 转存作业仍在 `queued`/`running` 的行不清理：先删行会让作业完成时更新 0 行，云端
  留下没有本地来源的记忆。这类行等作业结束后再按保留期回收。
- 清理与未提交批次回收（`BatchTTL`）在同一个 worker 循环里执行，随 `MEMOSD_LOCAL_SYNC_ENABLED=false` 一起停用。
- **软删除也要物理回收**：`DELETE /memories/{id}` 只置 `deleted_at`，配额立即释放但磁盘
  仍在。`MEMOSD_LOCAL_SYNC_DELETED_RETENTION`（默认 7 天）到期后由同一维护循环真正
  `DELETE`，否则“删除→再上传不同行”可以无限撑大共享库。未删除的行不受影响；正在
  `queued/running` 的转存行由保留期规则先跳过，等作业结束后下一轮再清理。

## 错误与边界情况

- 设备令牌无效、已吊销、滑动过期、账号禁用：401；成员被移除、角色降级、项目停用：403。每次请求都复核，不做跨请求的长缓存（只对 `last_seen_at` 做 1 分钟写节流）。
- 授权码不存在、过期、已使用、已拒绝：400/404（不区分“码不存在”与“码已过期”的细节文案），轮询接口统一返回可读原因；并发轮询只有一个成功，其余返回已使用。
- 批次不存在、不属于当前设备/项目、已过期：404；`seq` 非法或负数：400；覆盖同一 `seq` 允许，累计字节按差值调整。
- 块 gzip 解压失败：400；JSONL 某行不是合法 JSON：只拒绝该行。解压统一限制在单块 4 MiB 的压缩输入下，并用带上限的读取器防止压缩炸弹。
- commit 时某行 DB 写入失败（非配额）会让整批请求 500，批次保持 `pending`；重放幂等，不会产生重复行。
- 配额检查与写入在同一个事务里完成，避免并发批次越过上限；两个设备同时提交时由 SQLite 写连接串行化。
- 转存作业失败按既有 worker 语义退避重试，最终失败进入死信并写 `local_sync.memory.promote.background` 审计；`promote_status=failed` 后用户可再次发起（重新入队）。
- 上游不可用时转存返回排队成功、由队列重试；不伪造 `promoted_memory_id`。
- 配置校验失败（例如块上限大于批次上限或 `MEMOSD_MAX_BODY_BYTES`、TTL 非正、配额非正）在启动期拒绝。

## 测试与验证

- 服务/存储单测（真实 SQLite 临时库）：
  - 授权码：创建→批准→轮询发放令牌；过期、拒绝、重复消费、并发轮询。
  - 设备令牌：正确令牌通过；吊销后 401；滑动过期 401；账号禁用 401；移出项目/降为 viewer 后 403；跨项目访问 403。
  - ingest：分块上传与同 `seq` 覆盖；commit 重放返回同一结果且不重复写（行数不变）；内容未变不更新 `synced_at`；内容变化更新；部分行格式错误只拒该行；summary 模式出现原始正文整行拒绝；未声明层拒绝；manifest 额前行拒绝。
  - 限额：单行 256 KiB、单块 4 MiB、单批 64 MiB、项目 2 GiB / 50 万行分别返回 413；超出设备速率返回 429。
  - 删除单向：控制台删除后列表不再返回；本地再次上传同一内容不复活。
  - 转存：入队幂等（重复请求不产生第二个 job）；作业成功后 `promote_status=succeeded`；失败后可重试。
- transport `httptest`：设备授权码/轮询的状态码与信封（202 `code=20200`）、分块 `application/gzip`、commit 结果形状、Dashboard 六条路由的授权（viewer 读、editor 写/转存）、503 能力未开启。
- `internal/app` 黑盒 e2e：真实 SQLite + HTTP，覆盖设备授权→上传→commit→Dashboard 查询→删除→转存的闭环，以及跨项目 403。
- 本地单元测试；集群验证沿用 `docs/ARCHITECTURE.md` 的分层，转存链路需要真实上游，不可用时用假上游验证队列与审计，并在报告中标明。

## 参考来源

- 本文末尾的《设计决议》：D1–D10 的产品与边界决定，以及风险表。
- `docs/module/accounts.md`：会话、角色矩阵与 `Role.Can`/`Principal.Can`。
- `docs/module/store.md`：迁移、时间格式、读写连接池与事务边界。
- `docs/module/worker.md`：job 状态机、退避与死信语义。
- `docs/module/audit.md`：审计字段与异步队列。
- `docs/module/dashboard-routes.md`：Dashboard 信封、授权规则与审计写法。

## 实现记录

See the public version-control history.
## 设计决议（D1–D10）

状态：已确认（2026-10-24）。**D2 的边界经用户明确确认**：控制面扩展为可以持有客户端上传来源的记忆正文，但只用于展示、审计与显式转存，检索/RAG 仍归 memos-api。

- **D1 分开存储，统一呈现，显式转存**：云端新增只读镜像，不写进上游抽取管线；控制台并列展示本地镜像与云端记忆；“混合”只通过逐条/批量转存实现，成功与否可追溯，重复转存幂等。
- **D2 控制面持有镜像，但不承担检索**：这是对“控制面只存元数据”的显式例外。镜像存在同一个 SQLite 里，`payload` 是 gzip 后的原始 JSON，只用于展示、审计与转存，不喂给上游，也不作为控制面的检索数据源。
- **D3 两条入口、一个内核**：插件推送用设备令牌（`local_sync:write`），手工上传用账号会话；两者解析同一个 bundle，走同一套校验、配额、幂等与审计。手工上传落到合成设备 `manual`。
- **D4 绑定走设备授权码**：OAuth Device Flow 风格；零信任网关已负责公司级 SSO，不再引入第二套账号。设备令牌只存 SHA-256、180 天滑动过期、按设备吊销，只能访问 `/api/local-sync/*`，每次请求复核成员关系与角色。
- **D5 默认不上传**：插件侧总开关默认关闭，四层各自独立且默认不勾选；Trace 默认只传摘要，`full` 需二次确认；默认只传 `private` 行；首次上传前展示将离开本机的内容清单。
- **D6 传输可增量、可重放、可部分失败**：`state`/`batches`/`chunks`/`commit`/`manual` 五个入口；`batchId` 幂等、`seq` 覆盖重发、行级幂等键、单行失败不整批失败、删除不双向。
- **D7 存储与配额**：独立于上游的表；单项目 2 GiB / 50 万行；`payload` 存 gzip，列表不读大字段；所有 ingest/删除/吊销/转存写审计。
- **D8 控制台交互**：项目内“本地记忆”页与“本地设备”页；空态、未开启、超配额分别有显式文案；治理摘要展示 pending 占用。
- **D9 插件侧**：`core/cloud-sync/` 负责 bundle 构建、设备授权、分块上传、重试与 outbox；推送失败不影响本地采集；Viewer 展示绑定、层开关、预估、上次结果与真实错误。
- **D10 不做什么**：不改 MCP 的 5 个工具契约；不做云端 → 本地反向同步；不把 embedding 上云；不改插件 hub 语义；不自动把原始 trace 灌进上游抽取。

### 风险与处置

| 风险 | 处置 |
| --- | --- |
| 原始 trace 含密钥/业务数据 | 默认 summary；full 二次确认；插件侧提供脱敏预检（off/warn/redact，默认 warn）；服务端明确不做可靠脱敏 |
| 全量首传体积大 | 分块 + 单块/单批/解压总量上限 + 项目配额；首传前显示预估 |
| 本地删除不传播 | 明确写为单向语义并在页面提示，不承诺双向同步 |
| 设备令牌泄露 | 只存哈希、按设备吊销、范围限死 `/api/local-sync/*`、滑动过期 |
| 控制面存正文超出原边界 | D2 显式决议（已获用户确认）+ 文档 + 只存不检索 |
| 上游不可用导致转存失败 | 走既有 job 队列重试与死信，页面可重试并显示真实原因 |
| 控制面库写放大 | 分块提交在单事务内落库；删除/过期维护按小时批量清理；部署清单预留磁盘与内存余量 |
