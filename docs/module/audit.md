---
module: audit
packages:
  - internal/service/audit
  - internal/platform/auditfile
status: implemented
---

# audit

记录每次工具调用与 REST 调用的审计行，字段对齐既有网关的 `calls-*.jsonl`，供 Dashboard 读取。写入异步、失败不阻断主路径。

## 职责与边界

- 负责：构造审计记录（ID、UTC 时间、call_id、保存 input/output/query）、**完整原文的 JSONL 归档**、异步队列与按条数/字节切批落库、批量失败降级、按天分批清理、查询过滤。
- 不负责：身份解析（identity）、HTTP 中间件的协议细节（transport 负责把上下文交给 audit）。

## 依赖

- 依赖 `internal/domain`、`internal/platform/logging`、`internal/platform/auditfile`。
- 存储端口在使用方声明，不 import adapter：

```go
type Store interface {
    InsertAudit(ctx context.Context, r domain.AuditRecord) error
    ListAudit(ctx context.Context, f Filter) ([]domain.AuditRecord, int, error)
    GetAudit(ctx context.Context, id string) (domain.AuditRecord, error)
    AuditHourCounts(ctx context.Context, f Filter) ([]domain.AuditHourCount, error)
    AuditIDsExist(ctx context.Context, ids []string) (map[string]struct{}, error)
    PurgeAuditBefore(ctx context.Context, before time.Time) (int64, error)
}
```

`Input`/`Output`/`Query` 不做脱敏：凭据类字段按原值落库（运营要求审计可还原当时的请求与响应；与日志链路"保留原始值"的口径一致），审计表因此按敏感数据管理。**完整原文另写 JSONL 归档**（`Options.File`），不受 256 KiB 上限约束；SQLite 查询副本仍有单条正文总量上限（`maxAuditPayloadBytes`，256 KiB）：超限时按三个字段各自大小比例分配额度，按 UTF-8 字符边界截断，并在文本末尾追加 `\n…[audit payload truncated: <N> bytes]` 标记。保留标记是为了让人一眼看出正文不完整、被截掉多少字节，避免把残缺内容当成原始事实；`ResultBytes` 表示上游结果大小，不随截断改变。`Filter` 与存储端口的 `AuditFilter` 同形：`TenantID`、`Tool`、`Tools`、`ExcludeTools`、`Sources`、`OK`、`Status`、`Text`、`Since`、`Until`、`Limit`、`Offset`。`ExcludeTools` 用于控制台按“网关调用 / 控制台操作”互补过滤，不需要把所有工具名重复列一遍。`Status` 过滤的是按 `call_id` 合并终态行之后的派生状态（`SUCCESS`/`FAILED`/`RUNNING`），`OK` 仍直接按行内 `ok` 过滤；控制台调用日志用 `Status`。`Text` 在请求行自身的 `input_json`/`output`/`query`/`err`/`session_id`/`tenant_id` 上做不区分大小写的子串匹配，并额外用相关子查询命中同一 `call_id` 的异步终态行 `err`/`output`，与 `Get` 的合并口径一致（否则同一个调用在列表里搜不到、点进详情却能看到错误与正文）。`Recorder.Get` 与 `Recorder.HourCounts` 透传给存储，供控制台的调用日志详情与用量聚合使用；`Recorder.List` 只返回元数据（`input`/`output`/`query` 为空），正文走 `Get`。模型 token 用量（`RequestTokens`/`ResponseTokens`/`LLMCalls`）落在异步 add 的终态行；请求行详情会按 `call_id` 合并终态行的用量，`HourCounts` 同时返回按工具聚合的 token 合计。0 表示上游没有回报，不是“用了 0 个 token”。

### 来源（source）

`tool` 回答“调了什么”，`Source` 回答“从哪进来的”。两者不能互相推导：同一个 `add_message`
既可能来自 MCP 工具调用，也可能来自 Hermes 记忆插件走的 `/memory/add` 门面，还可能来自
运维脚本直连 REST。只看工具名时这几类流量在调用日志里混成一堆，出不了“插件占用多少、
MCP 占用多少”的账。

插件与直连脚本走的是**同一批 REST 端点**，路径分不出来，所以由客户端在
`X-GraphMemory-Client`（及同值的 `User-Agent`）里自报 `graphmemory/<version>`：带标识记
`plugin`，不带的记 `rest`。这个头不参与鉴权，冒用只会让自己看起来像插件，拿不到权限。

| `Source` | 写入方 | 说明 |
|---|---|---|
| `mcp` | `mcp.Handler` / 工具调用 / 协议帧归档 | 唯一入口 `POST /mcp` |
| `plugin` | `rest.Server`，请求带插件自报标识 | Hermes 记忆插件 |
| `rest` | `rest.Server`，无标识 | 脚本、其它 REST 客户端 |
| `dashboard` | Dashboard 路由 wrap | `dashboard.*` 工具名 |
| `admin` | Admin 路由 wrap | `admin.*` 工具名 |
| `auth` | 登录/登出等身份入口 | `auth.*` 工具名 |
| 空 | 迁移 `0023` 之前的行、异步 add 的终态行 | 历史行没这个信息，不能事后推断 |

不变量：

- `source` 由**入口**写入，不由被调用的服务推断。service/worker 层拿不到请求来源，
  在那一层猜会把“谁在压上游”这件事猜错。
- 异步 add 的**终态行不重复写 `source`**：它与请求行同 `call_id`，列表按 `call_id` 合并、
  详情按 `call_id` 补终态用量，来源以请求行为准；重复记反而要保证两行永远一致。
- 过滤用 `Sources []string`（`source IN (…)`）。`scope=gateway` 仍按**工具名**取 MCP 与
  REST 的合集，不改成按 `source` 过滤：历史行 `source` 为空，按来源筛会把它们一起筛掉。
- `source` 只进 SQLite 与 JSONL 归档，不参与 `Text` 搜索：它是个枚举，用过滤条件表达比
  塞进全文匹配更清楚。

### MCP 原始请求归档

`mcp.Handler` 在鉴权**之前**读取 `POST /mcp` 的 body，写一条 `Tool=mcp.request` 的 JSONL 记录，只进归档、不进 SQLite：

- `Input.body`：客户端原始请求体文本（JSON 字符串转义，解码后与收到的文本一致；不保存 header、HTTP 报文头顺序与 JSON 之外的空白差异）。
- `Input.method`/`path`/`content_type`/`user_agent`/`body_bytes`：定位请求来源；`truncated`/`read_error` 表示 body 超过入站上限或读取失败。
- `ok`/`http_status`：鉴权通过且 body 可读为 `true/200`；无效 token 为 `false/401`；body 超限为 `false/413`。鉴权失败的记录没有 `tenant`/`user_id`。
- 覆盖范围是所有 `POST /mcp`，包括 `tools/call`、`initialize`、`tools/list`、`ping`、批量请求、坏 JSON 与未认证请求；`GET`/`DELETE` 没有 body，不写该类记录。
- 工具调用的参数/结果仍由 `invoke` 另写一条工具审计；协议帧另写 `mcp.<method>` 行。三者可以按 `ts`、`request_id` 和 body 里的 JSON-RPC `id` 关联。

## 契约

```go
type Options struct {
    QueueSize     int           // 默认 1024
    RetentionDays int           // 来自 config.Audit.RetentionDays；<=0 不清理
    FlushInterval time.Duration // 默认 1s
    PurgeInterval time.Duration // 默认 1h
    StatsInterval time.Duration // 默认 30s，队列统计的汇报周期
    File           string       // 非空时追加完整 JSONL；打不开则构造失败
    FileMaxSizeMB  int
    FileMaxBackups int           // 0 表示保留全部轮转文件
    FileMaxAgeDays int           // 0 表示不按天数删除
    FileCompress   bool
}

type Recorder struct{ /* ... */ }

func New(store Store, log *slog.Logger, opts Options) (*Recorder, error)

// Record 先同步追加完整归档，再非阻塞入队；ctx 只用于取请求 ID，取消与否不影响写入。
// 队列满或超字节预算时 SQLite 副本丢弃、计数并记 WARN，但归档已经落盘。
func (r *Recorder) Record(ctx context.Context, rec domain.AuditRecord)

// RecordNow 同步写入，用于后台任务完成行等必须落库的场景。
func (r *Recorder) RecordNow(ctx context.Context, rec domain.AuditRecord) error

// RecordArchive 只追加完整 JSONL 归档，不进入 SQLite；用于 MCP 原始请求、
// 协议帧等只需要审计留痕、不需要控制台查询的记录。
func (r *Recorder) RecordArchive(ctx context.Context, rec domain.AuditRecord)

// Run 消费队列并定期清理过期记录，直到 ctx 取消；返回前排空队列。
func (r *Recorder) Run(ctx context.Context) error

func (r *Recorder) List(ctx context.Context, f Filter) ([]domain.AuditRecord, int, error)

// Stop 停止接收并排空（带超时），可重复调用；成功后关闭归档文件。
func (r *Recorder) Stop(ctx context.Context) error

// Close 关闭归档文件；App.Close 在 Run 返回后调用。
func (r *Recorder) Close() error

// FileFailures 返回归档写入失败条数，供指标采样。
func (r *Recorder) FileFailures() int64
```

字段映射（`domain.AuditRecord` ↔ 网关 JSONL）：

| JSONL | 记录字段 | 说明 |
|-------|----------|------|
| `ts` | `TS` | 真 UTC；请求行为请求开始时间，后台任务为记录完成行的时间 |
| `tool` | `Tool` | `add_message` / `search_memory` / `delete_memory` / `add_feedback` / `get_user_profile` |
| `source` | `Source` | 调用入口：`mcp` / `plugin` / `rest` / `dashboard` / `admin` / `auth`；迁移 `0023` 之前的行与后台终态行为空（见下节） |
| `call_id` | `CallID` | 16 字节随机 hex；缺省时生成。异步 add 的请求行 `call_id` 会作为 job ID 传给 worker，终态行与它相同；幂等重放沿用首次请求的 `call_id` |
| `deferred` | `Deferred` | add 异步时工具行只记入队耗时 |
| `tenant` | `TenantID` | 与既有网关日志同名，存项目/租户；`user_id` 另列 |
| `session_id` | `SessionID` | |
| `case_like` | `CaseLike` | 三态：`nil` 表示不适用 |
| `ms` | `Ms` | |
| `ok` / `err` | `OK` / `Err` | `err` 只放错误类型名，不放正文 |
| — | `ErrMessage` | 可对外的安全错误消息（领域错误的 `Message`），不含上游地址与 SQL 细节；迁移 `0019` 起落库 |
| — | `HTTPStatus` | 这次调用真实的 HTTP 状态（MCP 工具失败仍是 200，后台任务成功 200 / 失败 500）；迁移 `0019` 起落库 |
| `result_bytes` | `ResultBytes` | |
| `input` / `output` / `query` | 同名字段 | 不脱敏；JSONL 归档保存完整原文，SQLite 查询副本超过单条 256 KiB 时按 UTF-8 边界截断并追加标记 |
| — | `RequestID` | `X-Request-Id`（`httpx.RequestID` 中间件生成或透传）；`Record` 从 ctx 自动取，没有则为空 |
| — | `RemoteIP` | TCP 对端 IP，不信任 `X-Forwarded-For`；由 transport 从请求或 ctx 取，后台任务为空 |

不变量：

- 审计写入失败只记日志，不影响业务响应。
- 归档优先：`Record` 在入队前把**完整**记录（已补 ID/call_id/TS/RequestID）追加到 JSONL；队列满、字节预算超限或 SQLite 写失败都不会回滚已归档的行。文件写入是同步的，单次本地 PVC 追加在毫秒级；磁盘满时记 ERROR、累计 `memosd_audit_file_write_failed_total`，业务继续，SQLite 副本仍尝试写入。`RecordNow` 与 `audit.dropped` 汇总行也走同一归档。
- 归档轮转由 `lumberjack` 执行：单份 1GB、`MaxBackups=0`、`MaxAge=0`、压缩；**不按份数或天数删除**。本集群没有 Prometheus，容量没有自动告警，需要人工巡检 `memory-console-logs` 并在接近上限时扩容。访问日志在 prod 同样改为不删除。JSONL 的 `output` 若是合法 JSON 会压缩成单行结构，否则按字符串转义，保证一条记录只占一行。
- 只有认证成功的主体才写审计：MCP/REST 的无效 token 请求不入队（Dashboard/Admin 早已如此），避免无凭据流量放大写队列；REST 因此先鉴权再解析 body。
- 队列满时丢弃并累计计数，`Record` 不阻塞、不 panic。
- 队列同时按估算字节限流（64 MiB）：单条记录含完整 input/output，可按条数装下但总量远超 Pod 内存；超预算与队列满一样丢弃并计数，宁可丢审计也不先 OOM。
- 单条 `input` + `output` + `query` 的估算总量上限为 256 KiB（`maxAuditPayloadBytes`）。超限时先按字段大小比例分配额度并给非空字段保留 1 KiB 最小份额，再按 UTF-8 字符边界截断；`Input` 内部递归处理，优先保留小字段，字符串同样带标记。截断不改变 `ResultBytes`。
- 上限是**估算**，不是精确的落库字节数：`recordBytes` 对字符串按 UTF-8 字节计，对其它容器类型（数字、嵌套 map、`json.RawMessage` 等）按固定小常量计，不递归求真实序列化尺寸。因此正常 JSON 输入落到 256 KiB 附近时实际可能略超；写入时 `input_json` 不做 HTML 转义（`json.Encoder.SetEscapeHTML(false)`），否则 `&`、`<`、`>` 会被转义成 `\u0026` 这类 6 字符序列，单字符从 1 字节膨胀到 6 字节，让估算彻底失真。这里的约束是“防止单条记录拖垮写链路”，不是精确配额；需要硬上限时应改为序列化后按真实字节再截。
- `flush` 同时受两个阈值约束：单批不超过 50 条、估算字节不超过 4 MiB（`maxBatchBytes`）。消费循环达到任一阈值就提前提交；`flush` 自身也会再切一次，保证停机排空时不会把整队记录塞进一个事务。单条记录本身超限时独占一批，宁可慢也不拆记录。
- 整批写入失败后逐条单独插入重试一次：成功的计入 `written`，仍失败的逐条计入 `writeFailed`；一次降级只打一条汇总 WARN（不带每条记录的错误），避免故障时刷屏。整批失败但逐条全部成功时 `writeFailed` 不增加。
- **丢弃与写入必须可观测、可持久追溯**（指标、日志与审计表三处出口）：
  - 每次丢弃仍立即记 WARN（首次与每 100 条一次），消息区分 `queue full` 与 `over byte budget`，带 `dropped_total`、队列深度与已用字节。
  - 统计窗口内只要有丢弃或写失败，就同步向审计表补一条 `Tool=audit.dropped` 的汇总行（`OK=false`，`Input` 带同一组计数），使审计缺口在数据库里同样留痕；该行不受 `log/list` 与用量统计影响（它们只认 5 个对外工具），也不属于任何租户。
  - 计数器在进程内，重启归零；摘要的累计值配 `dropped_total` 与窗口增量，重启边界可从日志时间戳看出。
  - Prometheus 侧另有 `memosd_audit_written_total`、`memosd_audit_write_failed_total` 与 `memosd_audit_last_write_timestamp_seconds`（`Recorder.Counters()` 采样）。本集群没有部署 Prometheus，这些指标只在临时 port-forward 时手工查看；运行期判断以 30s 一条的 `audit queue stats` 日志和 `audit.dropped` 汇总行为准。
- 审计表是敏感数据：`input` 来自请求体、`output` 是完整响应，可能含凭据类字段（`apiKey`、`token`、`Authorization` 等）与记忆正文；这些值只落审计表，不得复制进 INFO 及以上日志，也不得下发给审计权限之外的主体。
- `Run` 退出前必须排空已入队记录（最多 `ShutdownTimeout`）。

## 错误与边界情况

- `Record` 在 `Recorder` 未启动 `Run` 时也能入队；`Stop` 之后调用静默丢弃（停机期间不再刷日志）。
- 清理按 `TS < now - RetentionDays*24h` 删除，返回删除总条数。`PurgeAuditBefore` 以 5000 行为一批循环删除，每批独立提交，尊重 ctx 取消（取消后返回已删除数量与 `context.Canceled`）；每批之后尽力执行 `PRAGMA wal_checkpoint(PASSIVE)`，整轮成功结束后由 `purgeLoop` 调用存储端的 `Optimize`（`PRAGMA optimize`）。不在一个事务里删完整轮，避免长事务与 WAL 暴涨；停机导致的取消不记 WARN。
- 异步 add 的终态行由 worker 在任务进入终态时用 `RecordNow` 同步写一条，`Tool=add_message.background`（与请求行的 `add_message` 分开，控制台的调用日志与用量只统计请求行，不把一次异步 add 数成两次）。`CallID` 用 `job.ID`，而 `job.ID` 由 transport 生成的请求行 `call_id` 传入（`AddRequest.CallID`），两者天然相等；重试复用同一 job ID，但**中间重试不写审计行**（每轮失败仍留在结构化日志里），只有成功（`OK=true`）或最终失败（不可重试 / 达到 `MaxAttempts`，`OK=false`）才写，因此同一 `call_id` 至多一条终态行。
- 控制台按 `call_id` 合并终态行：请求行是逻辑调用，`total` 只计请求行；有终态行 `ok=true` → `SUCCESS`，有终态行 `ok=false` → `FAILED`；没有终态行时，`deferred=true` 且**同一 `call_id` 的 job 仍在库里**才是 `RUNNING`，否则按请求行自身 `ok` 回退为 `SUCCESS`/`FAILED`。job 由 worker 按保留期清理（默认 7 天，`PurgeFinishedJobsBefore`），清理后仍在审计保留期内的请求行因此会从 `RUNNING` 收敛到行内 `ok`；升级前落库的请求行 `call_id` 与 `job.ID` 是两次生成、永远匹配不到，也就直接按行内 `ok`，不会在 30 天审计保留期内显示成一批“卡住”的调用。非 deferred 的请求行始终按自身 `ok`。终态行本身不属于任何逻辑调用，不进入列表也不计数。派生状态在存储层用同一 `call_id` 的标量子查询与 `jobs` 存在性判断计算，`status` 过滤、分页与计数都在数据库内完成（不能先分页再在内存里合并）。
- 幂等重放（同租户、同归一内容在 TTL 内命中 `accepted`/`succeeded` 记录）不会新建任务：`AddResult.CallID` 带回首次请求实际使用的 `call_id`，transport 用它覆盖重放请求行的 `call_id`，重放请求与原任务因此共用同一条终态行。`accepted` 重放在原任务结束前显示 `RUNNING`，结束后自动变成真实终态（`SUCCESS`/`FAILED`）；`succeeded` 重放直接显示首次的终态。它表示"去重到已有任务"，不是一次新的后台执行。**升级边界**：本次修复前落库的幂等缓存（响应体里没有 `call_id`）在部署后的一个 TTL（默认 24h）内仍可重放，这些重放行会显示 `RUNNING` 直到记录被清理；有界且一次性，需要立即消除时清一次过期 `idempotency_keys`。
- 存储不可用时队列积压到上限后丢弃，不无限增长。
- 汇总行只在有缺口（丢弃或写失败）的窗口生成；窗口内没有缺口就不写，避免审计表被正常流量灌满统计行。汇总行写失败只记 WARN，不会反过来影响队列消费。
- `RemoteIP` 取 TCP 对端（与访问日志同一口径），不解析 `X-Forwarded-For`：入口是零信任网关，转发链上的值可被调用方伪造，来源留 TCP 对端更可信。MCP 的 ctx 由 `mcp-go` 从 HTTP 请求透传，因此同样拿得到。

## 测试与验证

- 假 Store：`Record` 后 `Run` 能落库；队列满时丢弃计数；`Stop` 排空；`List` 透传过滤条件；清理调用参数正确。
- 小 payload 不改写：未超 256 KiB 的 output/query 与含 `apiKey`/`Authorization`/`client_secret` 的嵌套 input 逐字节一致（不脱敏）。
- 截断：三个字段合计超过 256 KiB 时，各字段按比例截断，结果仍是合法 UTF-8、末尾带 `audit payload truncated` 标记，`ResultBytes` 不变，`Input` 里的短字段（如计数）不被超大字符串挤掉。
- 按字节切批：构造 20 条接近 256 KiB 的记录，断言 `InsertAudits` 被调用多次、每批不超过 50 条且估算字节不超过 4 MiB、总条数不丢。
- 批量降级：第一次批量失败、逐条成功时全部落库且 `writeFailed` 不增加；逐条仍有失败时 `written`/`writeFailed` 分别正确，且只产生一条汇总 WARN。
- 清理：`PurgeAuditBefore` 在多于一批（5000 行）的数据上循环删除并返回总数；已取消的 ctx 返回 `context.Canceled` 且不删数据。
- 请求 ID：`Record(ctx)` 从 context 自动填 `RequestID`；context 没有 ID 时为空，不报错。
- 归档：600 KB 的 input/output/query 写入 JSONL 后逐字节完整，SQLite 副本仍是 256 KiB 截断；队列容量只有 1 时连发两条，第二条在 SQLite 被丢弃，但文件里两条都在；`New` 在路径不可写时返回错误。
- 统计：窗口内有丢弃时生成一条 `Tool=audit.dropped` 汇总行且计数正确；没有缺口时不写汇总行。
- 本地单元测试。

## 参考来源

- `AGENTS.md`：“日志按运营要求保留原始值，handler 不脱敏、不截断”；审计沿用“不脱敏”，但单条正文按 256 KiB 上限截断并留标记（避免单条记录拖垮写链路）。

### case_like 判定

```go
func LooksCaseSpecific(value any) bool
```

从待写入内容判断是否像个案结论（UUID、`uid`、告警号、文件哈希、IP、域名），正则与网关 `audit/extract.py` 一致。只看不拦，仅用于审计字段；mcp 与 rest 共用。

## 实现记录

See the public version-control history.
