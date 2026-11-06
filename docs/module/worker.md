---
module: worker
packages:
  - internal/worker
status: implemented
---

# worker

消费 `jobs` 表里的异步任务：`add_message` 的抽取写入。任务状态落库，停机先排空，失败按指数退避有限重试；按租户并发、同租户串行。

## 职责与边界

- 负责：轮询/认领任务、调用 memory 的 `RunAddJob`、状态迁移、失败退避与错误分类、回收崩溃遗留的 running 任务、停机排空。
- 不负责：任务入队（memory 经 `Jobs` 端口）、上游调用细节（upstream）、审计内容（transport）。

## 依赖

- 依赖 `internal/domain`、`internal/platform/logging`，以使用方接口依赖存储与业务：

```go
type Jobs interface {
    // 认领最早的可执行任务；同一租户同时只有一个 running。
    // 只有 next_attempt_at 不晚于 now 的 pending 任务可被认领。
    ClaimJob(ctx context.Context, now time.Time) (domain.Job, bool, error)
    // 回收超过 JobTimeout 仍未结束的 running 任务。
    ResetStaleJobs(ctx context.Context, now, staleBefore time.Time) (int64, error)
    // 把失败任务放回 pending 并指定下次可认领时间；行与 attempts 不变。
    RescheduleJob(ctx context.Context, p domain.JobReschedule) error
    FinishJob(ctx context.Context, p domain.JobCompletion) error
}

type Runner interface {
    RunAddJob(ctx context.Context, payload []byte) (domain.AddResult, error)
}
```

`Runner` 由 `service/memory` 实现（结构化满足），worker 不 import adapter。任务进入终态时，worker 用 `Audit.RecordNow` 写一条 `Tool=add_message.background` 的终态行：成功 `OK=true`，最终失败（不可重试或达到 `MaxAttempts`）`OK=false`；成功时把上游 `AddResult.Usage` 的 `prompt_tokens`/`completion_tokens`/`calls` 写入审计行的 `request_tokens`/`response_tokens`/`llm_calls`（上游没回报就是 0，控制台据此保持空）；中间重试不写审计行，每轮失败只留结构化日志。`CallID=job.ID`，而 `job.ID` 由请求行 `call_id` 传入，所以请求行与终态行天然同 `call_id`，控制台据此合并出 `SUCCESS`/`FAILED`/`RUNNING`。

## 契约

```go
type Recorder interface {
    RecordNow(ctx context.Context, rec domain.AuditRecord) error
}

type Options struct {
    PollInterval time.Duration // 默认来自 config.Worker.PollInterval
    MaxAttempts  int           // 默认 3
    Concurrency  int           // 同时消费的任务数，默认 4；同租户仍串行
    JobTimeout   time.Duration // 单任务超时，默认 5m
    RetryBase    time.Duration // 首次重试等待，默认 5s
    RetryMax     time.Duration // 单次重试等待上限，默认 10m
    RetentionDays  int           // 终态任务保留天数，默认 7
    IdempotencyTTL time.Duration // 幂等记录保留时长，默认 24h
    Audit        Recorder      // 可为 nil；只在任务进入终态时写一条完成行
}

type Service struct{ /* ... */ }

func New(jobs Jobs, runner Runner, log *slog.Logger, opts Options) *Service

// Run 启动 Concurrency 个消费者轮询并执行任务，直到 ctx 取消；返回前排空正在执行的任务。
func (s *Service) Run(ctx context.Context) error
```

状态机：

```text
pending --ClaimJob(next_attempt_at <= now)--> running --成功--> succeeded
                            --可重试失败且 attempts < MaxAttempts--> pending（同一行，next_attempt_at = now + 退避）
                            --不可重试失败或 attempts >= MaxAttempts--> failed
```

- 重试复用同一行：`RescheduleJob` 把状态改回 `pending`、写 `last_error` 与 `next_attempt_at`，job ID、`created_at`、`attempts` 都不变。退避为 `RetryBase * 2^(attempts-1)` 并叠加 0..50% 的抖动，封顶 `RetryMax`。
- 错误分类：只有能确认是永久错误的类型才不重试——`invalid_input`、`unauthorized`、`forbidden`、`not_found`、`conflict`、`disabled`、`upstream_denied`（上游 401/403/404/409）。`upstream_unavailable`、`rate_limited`、`upstream_rejected` 与未包装的内部错误仍重试；分类不确定时偏向重试。
- 终态回调：`Runner` 若同时实现 `OutcomeReporter`（`memory.Service` 实现了），任务成功或永久失败时用独立 5s 预算回调 `ReportJobOutcome`，让请求去重记录从 `accepted` 收敛到 `succeeded` 或删除；瞬时失败仍走重试，不回调。回调只对 `memory_add` 触发，其它 kind 不套用 add 的去重语义。
- 观测：`Observer.ObserveJob` 每次尝试记耗时；任务进入终态失败时额外调用 `ObserveJobExhausted(kind, reason)`，`reason=permanent` 表示不可重试错误、`attempts` 表示尝试次数用尽。
- `RunAddJob` 的 context 带 `JobTimeout`；停机时在途任务**不随父 ctx 取消**，用 `context.WithoutCancel` + `JobTimeout` 跑完，`FinishJob`/`RescheduleJob` 再用独立的 5s 预算，避免任务永久停在 `running`。
- 认领/执行错误：记录 WARN，不退出循环；连续错误只按轮询间隔退避。
- 停机：ctx 取消后不再认领；在途任务继续执行并记账（最多 `JobTimeout`）后返回。
- 清理：独立的清理循环启动时先跑一次，之后每分钟检查、每小时执行一次 `PurgeFinishedJobsBefore`（删除 `RetentionDays` 之前的终态任务）与 `PurgeIdempotencyBefore`（删除超过 `IdempotencyTTL` 的请求去重记录）；失败只记 WARN，不影响认领。`AddAsync` 关闭时 worker 仍启动，仅用于清理。

不变量：

- 同一租户同时只处理一个任务：`ClaimJob` 跳过已有 running 任务的租户；不同租户由 `Concurrency` 个消费者并行处理。SQLite 单连接保证认领事务串行，所以这个判断没有竞态。
- 任务负载只含租户与归一后的消息，不含明文 Token。
- 失败原因只记错误类型/安全消息，不记记忆正文。

## 错误与边界情况

- `ClaimJob` 返回空 → 等下一个轮询周期；`next_attempt_at` 未到期的重试任务也会让出消费者，不占着轮询。
- 退避期间该租户不被阻塞：重试任务是 `pending` 而不是 `running`，同租户的其它任务可以继续认领；到期后按 `created_at` 回到队首。
- 进程在 `running` 状态被杀：`updated_at` 超过 `JobTimeout + 5s` 仍未结束的任务会被下一次认领重置为 `pending` 并立即可认领，该租户最多阻塞这么久；`attempts` 保留，崩溃算一次尝试。
- `Runner` 返回不可重试错误（`invalid_input`、`unauthorized`、`forbidden`、`not_found`、`conflict`、`disabled`、`upstream_denied`）→ 直接 failed，不计退避。
- 不支持的任务类型（非 `memory_add`）→ 记 ERROR、一次即 failed、**不写 `add_message.background` 审计行、不回调终态**。只有历史脏行或编程错误会走到这里，因为入队侧只产生 `memory_add`。

## 死信与排查

`failed` 就是死信：任务已放弃重试，行保留到 `RetentionDays`（默认 7 天）后被清理。排查入口：

- `GET /admin/jobs?status=failed`（平台管理员）列出死信，可按 `kind`、`tenant_id` 过滤；响应不含 payload 正文。
- `memosd_worker_jobs{state="failed"}` 展示当前保留期内的死信条数；
  `memosd_worker_jobs_exhausted_total{kind,reason}` 记录新增的终态失败，用于告警。
- 审计表里的 `add_message.background` 终态行与任务共用 `CallID`，可按 job ID 定位最终成败；中间重试的失败只在结构化日志（`job failed` + `job rescheduled`）里，不在审计表留行。

**有条件重放（2026-10-26 起）**：`POST /admin/jobs/{id}/retry`（平台管理员，见 [admin](admin.md)）。
前提没变 —— 上游 `/product/add` 没有幂等键，「上游已写成功但响应丢失」无法与真失败区分。
所以重放前按 `jobs.last_error` 判 `domain.ReplaySafe`：

- **可以直接重放**：错误保证发生在请求被上游接受之前 —— `upstream rejected`、`upstream denied`、
  `invalid input`、`unauthorized`、`forbidden`、`conflict`、`not found`、`rate limited`、`disabled`
- **必须显式 `force=true`**：`upstream unavailable`（连不上/超时/5xx，可能已经写进去了）
  以及空串/未知类型。接口返回 409 并说明原因，由人确认上游确实没写成功后再重放

重放语义：`failed` → `pending`，`attempts` 归零，`last_error` 清空，`next_attempt_at` 置为
当前时间；行、job ID、`created_at` 不变（重放的是同一条任务，不是新建）。`pending`/`running`
再拉一次会重复写、`succeeded` 拉起来会写第二遍，所以只接受 `failed`。

**不做自动重放**：重试次数用尽就是让人看的信号；自动重放会把「上游一直 5xx」变成持续的
重复写入风险。批量重放也没做，需要就给同一接口循环调用（每次带不带 `force` 各自判断）。

**「受理即成功」是写入链路的对外语义**（2026-10-26 明确）：MCP `add_message` 与 REST
`/memory/add` 返回 2xx 就表示「已进入队列」，最终一致性由本模块的 worker 保证 ——
重试、终态审计行、死信可见、人工重放。调用方（含记忆插件）**不应**把 2xx 理解成「已写进上游」，
也**不需要**为写入结果轮询或给模型加状态查询工具：失败留在服务端、人可见可处理。
REST 的 `add` 在 2026-10-26 由同步改为异步，与 MCP 完全一致；两种入口的请求行都是
`deferred=true`，终态行由 worker 另写，来源（`mcp` / `plugin` / `rest`）只在请求行上。

### 实现记录

请参阅公开变更记录。

## 测试与验证

- 假 Jobs/Runner：成功路径、失败按退避重试到上限（断言 `next_attempt_at` 单调递增且不超过 `RetryMax`）、不可重试错误不重试、未知 kind 一次即失败且不产生 add 的审计/回调、ctx 取消后不再认领、不同租户并发执行。
- Store 层：同一租户的第二个任务在第一个 running 时不可认领，不同租户可同时认领，stale running 会被重置，`next_attempt_at` 未到期的任务不被认领。
- 真实 SQLite 集成：失败任务复用同一行退避重试，三次尝试的间隔确有增长，终态后不再被认领。
- 本地单元测试；第 2 层验证经 MCP `add_message`（异步）触发真实 worker。

## 参考来源

- `docs/ARCHITECTURE.md` 的“worker：异步写入、过期清理、崩溃遗留任务回收；状态落库，停机先排空”。
- `docs/module/store.md` 的任务表与 `ClaimJob`。

## 实现记录

See the public version-control history.
## 新增快照输出（2026-10-31，accepted）

完成审计在 `added_memory_ids` 旁保存可选 `snapshot` v1（契约见memory模块），保持调用ID与租户/用户一致。快照缺失不把已成功写入改为失败，不触发抽取重试。此字段仅供活动查询，不进入工具回执。新审计保留来自快照的会话标识以便后续准确关联。最终输出超过240KiB或快照序列化失败时整体丢弃快照、仅保存原确认ID，避免新增快照使原本可保存的ID输出被审计256KiB上限截成无效JSON。旧ID-only记录继续可读但不能展示伪造的历史正文。


业务事件身份恢复（accepted）：可选JobContextBinder.WithJobIdentity(ctx, job)在执行和终态回调前绑定队列真实ID/租户/用户，memory校验负载作用域一致并优先使用真实job.ID。旧pending负载缺call_id时仍可准确关联；脱离可信worker且缺ID时不猜造历史事件。该绑定不延长任务期限或改变原Runner/OutcomeReporter兼容端口。
