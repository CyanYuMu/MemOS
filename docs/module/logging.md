---
module: logging
packages:
  - internal/platform/logging
status: implemented
---

# logging

进程级 `log/slog` 日志器：JSON 输出、固定字段名、请求 ID 注入、按大小/时间落盘轮转。所有包通过注入的 `*slog.Logger` 记日志，不允许 `fmt.Print*` 或标准库 `log`。

## 职责与边界

- 负责：构造 `*slog.Logger`（级别、格式、输出目标）、把请求 ID 从 context 注入每条日志、按大小与时间轮转文件、落盘失败的可见性（stderr + 指标回调）、提供 `Redact` 辅助函数。
- 不负责：业务日志的内容与级别选择（各模块自己决定）、审计记录落库（audit 模块）、日志外送与跨节点集中存储。

## 依赖

- 标准库（`log/slog`、`context`、`io`、`os`）与 `gopkg.in/natefinch/lumberjack.v2`（文件轮转）。
- 被 `internal/app`、各 adapter/service/transport 使用。`httpx` 的访问日志用它。

## 契约

```go
type Options struct {
    Level      slog.Level
    Format     string    // "json" | "text"
    Writer     io.Writer // 默认 os.Stdout
    File       string    // 非空时同时落盘并按大小轮转
    MaxSizeMB  int
    MaxBackups int
    MaxAgeDays int
    Compress   bool
    // OnError 在文件写/刷失败时回调（op = "write" | "flush"）；为空时仍会写 stderr。
    OnError func(op string, err error)
}

func New(opts Options) (*slog.Logger, io.Closer, error)
func SetDefault(l *slog.Logger)
func WithContext(ctx context.Context, l *slog.Logger) context.Context
func FromContext(ctx context.Context) *slog.Logger   // 没有时返回 slog.Default()
func Redact(s string) string                          // 可选：前 4 + … + 后 4；长度 ≤ 12 时全掩码
```

不变量：

- 日志 key 一律 snake_case、静态消息；禁止把用户输入拼进 message。
- **保留原始值**：handler 不脱敏、不截断、不省略正文；`content`、`messages`、`query` 等字段在 INFO 级别也按原值落盘。这是为了事后审计与问题定位，代价是日志可能含业务正文。
- 凭据仍不得由业务代码主动记录。`sloglint` 在 lint 层禁用 `token` / `api_key` / `authorization` / `password` / `secret` 这些 key；确需在日志中展示凭据时，只能显式调用 `Redact`。
- 请求 ID：`httpx` 中间件把 ID 放进 context，handler 自动附加 `request_id` attr；若调用方已显式传 `request_id` 则以显式值为准。
- **分级**：只有全局阈值（`MEMOSD_LOG_LEVEL`：debug/info/warn/error，默认 info），没有按模块分级别，也没有采样。访问日志固定 `Info`；上游不可达、任务失败等可恢复问题用 `Warn`；启动失败、panic、关闭失败用 `Error`；`Debug` 目前只用于极少数排查点。日志量主要由每条请求一行访问日志决定。
- **持久化保证**：文件 sink 在构造期主动打开一次（不是等第一条日志），路径不可写直接启动失败；运行期写/刷失败不会被静默吞掉，会写一行 stderr 并以 `OnError` 上报指标，同一错误 60 秒内只报一次。Warn 及以上级别记录会立即 Flush，不留在 200ms 缓冲里。进程被 `SIGKILL`/OOM Kill 时最多丢失最近一次缓冲（≤64 KiB）；节点掉电仍可能丢页缓存，审计级留痕依赖外部采集。

## 错误与边界情况

- `Options.Writer` 为 nil → 用 `os.Stdout`；`Format` 未知 → 退回 JSON（构造期不 panic）。
- `File` 非空时 stdout 与文件双写：`kubectl logs` 照常可用，文件用于事后分析。目录不存在会自动创建；目录创建或文件打开失败则启动失败（fail fast）。
- 轮转按大小（`MaxSizeMB`，默认 1024）、保留份数（`MaxBackups`，默认 5）与天数（`MaxAgeDays`，默认 60），可压缩。**prod 把份数与天数都设为 0**，即 1GB 轮转但全部保留，不自动删除；dev 用 128MB×2、保留 7 天。保留全量后没有自动容量告警（集群未部署 Prometheus），需人工巡检日志 PVC，扩容而不是删历史日志。
- 运行期落盘失败（磁盘满、PVC 只读）不阻断业务，但会持续写 stderr 并累加 `memosd_log_write_errors_total{op}`；`op` 只有 `write` / `flush` 两个值，避免高基数。失败后会重置缓冲并清掉 bufio 的粘性错误，磁盘腾出空间后自动恢复写入，不需要重启进程。
- `Redact("")` 返回空串，不返回 `…`。

## 测试与验证

- handler 单测：JSON 输出包含 `level`/`msg`/`time`；敏感键与正文在 INFO 下原样保留；`request_id` 自动注入且显式值优先。
- `Redact`：短串全掩码、长串保留首尾、空串。
- 文件 sink：stdout 与文件同时收到记录，文件可读。
- 启动期 fail fast：日志路径不可写时 `New` 返回错误。
- 落盘失败可见性：注入失败的 writer 后，`OnError` 收到 `write`/`flush` 回调且 stderr 有提示；同一错误 60 秒内只上报一次。
- 失败恢复：writer 先失败一次后恢复，后续记录能重新落盘（验证 bufio 粘性错误被 Reset）。
- Warn 及以上立即刷盘：写入一条 `Warn` 后无需 `Close`，文件里已能看到该记录。
- 本地单元测试。

## 参考来源

- `AGENTS.md` 的“日志用 log/slog”与“凭据不得主动写入日志”。

## 实现记录

See the public version-control history.
