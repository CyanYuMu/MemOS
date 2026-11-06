---
module: observability
packages:
  - internal/platform/metrics
status: implemented
---

# observability


## 职责与边界

- 负责：进程指标暴露、上游/worker/审计的关键状态观测、与未来 Prometheus/Alertmanager 的对接约定。
- 不负责：审计记录（`audit` 模块，业务事实）、请求日志内容（各 transport）、集群监控组件本身的部署。
- 不负责：日志文件持久化（`logging` 模块）与日志外送；**日志和审计的持久化不依赖本模块**。

## 依赖

- `github.com/prometheus/client_golang`（`/metrics`、Go runtime 与 process collectors）。
- 告警规则文件 `deploy/prometheus/memosd-rules.yaml` 是可选模板；通知渠道由未来的 Alertmanager 配置，仓库不保存 webhook。
- Tracing 若启用，再引入 OpenTelemetry Go SDK + OTLP/HTTP exporter；当前零依赖。

## 契约

指标前缀 `memosd_`，标签只放低基数维度，**不放 tenant_id、memory_id、user_id、原始路径**：

| 指标 | 类型 | 标签 | 用途 |
|------|------|------|------|
| `memosd_http_requests_total` | counter | `role` `method` `route` `status` | 入口流量与错误率；`route` 取 `http.Request.Pattern`，未命中归 `other` |
| `memosd_http_request_duration_seconds` | histogram | `role` `method` `route` | p50/p95/p99；桶到 300s |
| `memosd_upstream_requests_total` | counter | `endpoint` `outcome` | `outcome` = ok / invalid / rejected / denied / rate_limited / unavailable / error；`endpoint` 中 id 收敛为 `{id}` |
| `memosd_upstream_request_duration_seconds` | histogram | `endpoint` | 上游慢请求 |
| `memosd_memory_operations_total` | counter | `op` `result` | add/search/delete/feedback/profile |
| `memosd_worker_jobs` | gauge | `state` | pending/running/failed 积压（15s 采样）；failed 为保留期内的死信条数 |
| `memosd_worker_oldest_pending_seconds` | gauge | — | 队列是否卡住（含退避中的 pending） |
| `memosd_worker_job_duration_seconds` | histogram | `kind` `result` | 异步写入每次尝试的耗时与失败率 |
| `memosd_worker_jobs_exhausted_total` | counter | `kind` `reason` | 进入终态失败的任务数；`reason` = permanent / attempts |
| `memosd_audit_queue_depth` / `_capacity` / `_bytes` | gauge | — | 审计队列积压（15s 采样） |
| `memosd_audit_dropped_total` | counter | `reason` | 审计丢弃，`reason` = queue_full / byte_budget |
| `memosd_audit_written_total` | counter | — | 成功落库的审计条数；判断审计是否还在产出 |
| `memosd_audit_write_failed_total` | counter | — | 写库失败（含整批回退后仍失败）的条数，失败记录不重试，即审计缺口 |
| `memosd_audit_file_write_failed_total` | counter | — | 完整原文 JSONL 归档写入失败条数；非 0 表示长期归档出现缺口 |
| `memosd_audit_last_write_timestamp_seconds` | gauge | — | 最近一次成功落库的 Unix 时间；从未写入时为 0 |
| `memosd_db_open_connections` | gauge | `state` | SQLite 写/读连接池 in_use / idle |
| `memosd_db_wait_count_total` / `memosd_db_wait_duration_seconds_total` | counter | — | 连接池等待 |
| `memosd_log_write_errors_total` | counter | `op` | 日志落盘写/刷失败，`op` = write / flush；非 0 表示审计留痕可能中断 |
| `memosd_chat_slots` | gauge | `state` | 对话进程槽位：total / running / idle / stopping；`idle` 是驱逐候选 |
| `memosd_chat_evictions_total` | counter | — | 为腾出槽位驱逐空闲会话的次数 |
| `memosd_chat_busy_total` | counter | — | 所有槽位都在生成、准入被拒的次数 |
| `memosd_chat_admission_seconds` | histogram | — | 一次准入（含等待与驱逐）的耗时 |
| `memosd_chat_pi_rss_bytes` | gauge | — | 全部在跑 pi 进程的常驻内存合计；每分钟随回收周期采样 |
| `memosd_build_info` | gauge(const 1) | `version` `role` | 发布核对 |
| `go_*` / `process_*` | — | — | client_golang 默认 collectors |

`/metrics` 走独立监听地址 `MEMOSD_METRICS_ADDR`（默认关闭），不放进 console Service，也不复用业务鉴权；生产清单设为 `:9090`，可在没有 Prometheus 时用 port-forward 手工查看。


## 错误与边界情况

- 指标记录 best-effort：计数与直方图在请求路径上只做内存操作，不加锁、不阻塞；采样失败只跳过本次。
- `MEMOSD_METRICS_ADDR` 未设置时不监听端口，但进程内仍会记录（用于测试与后续挂载）；不构成对外暴露。
- 高基数保护：`route` 必须来自 ServeMux 模板，404 与静态资源回退统一为 `other`；上游 `/product/get_memory/{id}` 收敛为 `{id}`。
- 指标地址不能与 console / mcp / pprof 相同，启动期校验。
- 审计丢弃既写 WARN 日志与审计汇总行，也累加 `memosd_audit_dropped_total`；有采集端时告警以指标为准，没有采集端时以落盘 WARN 日志为准。
- 审计写入失败后记录被丢弃、队列随之清空，只看队列深度会漏报；有 Prometheus 时用 `memosd_audit_written_total` 与 `memosd_memory_operations_total` 判断“有调用但无产出”，没有时看 30s 一条的 `audit queue stats` 日志。
- 进程刚启动、还没有任何业务调用时 `memosd_audit_last_write_timestamp_seconds` 为 0，这是正常状态；相关规则因此以“有调用 + 无写入”为条件，而不是仅看时间戳陈旧。
- 日志分级：全局阈值（`MEMOSD_LOG_LEVEL`），访问日志固定 info；当前不做按模块分级或采样。
- `request_id` 目前只在控制面日志与审计内使用，**尚未透传给 memos-api**；跨服务关联仍是待决事项。
- Tracing 只在存在 collector 时才有价值；上游 memos-api 未接入 OTel 时控制面只能产生自己的 span，因此暂不实现。

## 测试与验证

- `internal/platform/metrics`：记录各类指标后抓取 `/metrics`，断言序列与标签；nil 接收者不 panic；`ResultOf` 的错误分类。
- `internal/transport/httpx`：带 `{id}` 的路由模板被记录为模板本身，未命中路径归 `other`。
- `internal/app`：配置 `MEMOSD_METRICS_ADDR` 后 `Handlers()` 含 `metrics`，访问 `/metrics` 返回 200 且包含 `memosd_build_info` 与最近一次 `/healthz` 的计数。
- `internal/service/audit`：`Counters()` 在成功写入后累加条数并推进 `lastWrite`，全部写入失败时保持为零；`FileFailures()` 在归档文件关闭后写入失败时累加。
- `internal/adapter/store`：`JobStats` 的 pending/running 计数与最老 pending 时间。

## 参考来源

- `internal/transport/httpx/middleware.go`：访问日志字段与 `http.Request.Pattern` 路由模板。
- `internal/adapter/upstream/upstream.go`：上游错误分类（`ErrInvalidInput` / `ErrUpstreamRejected` / `ErrUpstreamUnavailable`）与端点收敛。
- `internal/worker/worker.go`：任务声明、重试与执行计时。
- `internal/service/audit/audit.go`：异步队列、字节预算与丢弃统计。
- `internal/adapter/store/job.go`、`store.go`：任务积压与 `sql.DBStats`。
- `deploy/prometheus/memosd-rules.yaml`：告警规则。
- `docs/module/logging.md`、`docs/module/deploy.md`：日志落盘与 PVC 轮转口径。

## 实现记录

See the public version-control history.
## 待决事项

1. **抓取方式**：集群当前没有 Prometheus；将来接入时再决定用 Pod 注解还是 ServiceMonitor。
2. **告警渠道**：集群当前没有 Alertmanager；接入时再决定流群 webhook、邮件还是平台告警。
3. **Tracing**：是否有 Jaeger/Tempo/OTLP collector，以及 memos-api 是否愿意一起接 OTel。
4. **日志外送**：hostPath 日志随节点/PVC 走，是否需要 Loki/ES 外送。
- 2026-10-27（参数收口）：`Metrics.ObserveHTTP` 改为接收 `metrics.HTTPObservation{Role, Method, Route, Status, Duration}`；httpx 的 `Observer` 接口同步调整。指标名、标签集合与 route 兜底 `other` 的口径不变。验证：`go test ./internal/platform/metrics ./internal/transport/httpx` 通过。
