---
module: bootstrap
packages:
  - cmd/memosd
  - internal/platform/version
  - internal/app
status: implemented
---

# bootstrap

进程入口 `memosd`（源码与本地二进制名；生产镜像里叫 `memory-console`）、构建期版本信息，以及组合根 `internal/app`：按角色装配依赖、启动各自独立的 `http.Server`、处理信号与优雅停机。

## 职责与边界

- `cmd/memosd`：解析子命令与参数，调用 `internal/app`。入口函数 `run(args, stdout, stderr) int` 不读全局状态、不直接退出，便于测试。
- `internal/platform/version`：`Version` / `Commit` / `Date` 三个变量，由 `make build` 通过 `-ldflags -X` 注入，默认 `dev` / `none` / `unknown`。
- `internal/app`：**唯一**允许同时 import adapter / service / transport 的包；负责依赖接线、角色到 `http.Server` 的映射、信号驱动的优雅停机。业务判断不得写在这里。
- 不负责：配置解析（config）、日志构造（logging）、路由与协议（transport 各模块）。

## 依赖

- `cmd/memosd` 只依赖 `internal/app`、`internal/config` 与 `internal/platform/{logging,version}`（depguard 的 `layer-cmd` 规则强制），不直接 import adapter/service/transport。
- `internal/app` 依赖 config、logging、domain、adapter、service、transport、worker；具体接线按模块逐步补齐。

## 契约

### 命令行

```text
memory-console version
memory-console serve [--role=console|mcp|all] [--console-addr=:5280] [--mcp-addr=:8002]
memory-console chown <dir>
```

- `version`：stdout 输出 `<Version> (commit <Commit>, built <Date>)`，退出码 0。
- `serve`：先 `config.Load()`，命令行传入的 `--role` / `--addr` 覆盖同名配置；解析失败或校验失败时向 stderr 输出脱敏错误，退出码 2。
- `chown`：把目录及内容改为 UID/GID 65532，供 local-storage 的 initContainer 修正 hostPath 属主；参数个数不对退出码 2。
- 无参、未知子命令、未知参数：stderr 输出 `usage: memory-console <version|serve|chown <dir>>`，退出码 2。
- 运行期致命错误（端口占用、Shutdown 超时）→ 退出码 1。

### 角色与监听

| 角色 | 监听 | 挂载 |
|------|------|------|
| `console` | `HTTP.ConsoleAddr` | Dashboard `/api/dashboard/*`、`/api/meta`、`/api/health`、`/api/user/*`、Admin `/admin/*`、Auth `/__login` `/__logout` `/__relogin` `/__invite/*` `/api/auth/*`、`/healthz`、`/readyz`，以及配置了 `WebDir` 时的 SPA |
| `mcp` | `HTTP.MCPAddr` | MCP `/mcp`、REST `/memory/*`、`/healthz`、`/readyz` |
| `all` | 两个地址 | 以上全部 |
| 可选 | `HTTP.PprofAddr` / `HTTP.MetricsAddr` | 独立监听的 `/debug/pprof/*` 与 `/metrics`；默认关闭，不进入业务 Service |

`all` 使用两个独立的 `http.Server` 与两个独立端口，不是一个端口挂全部路由：拆分角色时只改部署，不改路由。

```go
type App struct { /* 未导出字段 */ }
type Options struct {
    Config  config.Config
    Logger  *slog.Logger
    Metrics *metrics.Metrics // 可为 nil；入口进程先构造以接住日志落盘错误
}
func New(opts Options) (*App, error)
func (a *App) Run(ctx context.Context) error
func (a *App) Close() error
```

### 启动与停机

1. `New` 只做构造与校验（打开 store、跑迁移、构造各 handler），不监听端口。迁移用 `Store.MigrationTimeout` 的独立预算（`context.WithoutCancel`），其余装配步骤另起 30s 窗口。
2. `Run` 先启动 worker、指标采样与审计消费，再为每个角色启动 `httpx.NewServer`，在 goroutine 中 `ListenAndServe`；任一 server 返回非 `http.ErrServerClosed` 的错误时取消内部 context 并返回该错误。
3. `ctx` 被取消（`main` 里由 `signal.NotifyContext` 响应 SIGINT/SIGTERM）→ 对每个 server 并发调用 `Shutdown`，超时 `HTTP.ShutdownTimeout`。
4. 停机顺序：先停入站监听 → 等待审计队列排空（最多 `ShutdownTimeout`）→ 停止 worker 认领并等待在途任务记账（`JobTimeout + 5s`）→ 等待 server goroutine → `Close` store 与 Redis。
5. 日志：启动时 INFO 记 `role`、监听地址、版本；停机时 INFO 记原因与耗时。不打印配置原文。

### 健康检查

- `/healthz` 与 `/readyz` 在每个角色的 server 上都存在，语义见 httpx 模块。
- `readyz` 的检查项由 `app` 注册：`store`（读连接池 ping）与 `upstream`（`adapter/upstream.CheckContract`，校验契约版本、时间口径、sources 默认值与 `top_k_max`；旧镜像没有契约端点时放行）。
- 可选独立监听：`MEMOSD_METRICS_ADDR` 只暴露 `/metrics`；`MEMOSD_PPROF_ADDR` 只暴露 pprof。两者都不进入 console/mcp 的 Service，也不经过业务鉴权。

## 错误与边界情况

- `role=all` 时同一地址被两个角色占用（例如 `--console-addr` 与 `--mcp-addr` 相同）→ 启动前返回错误，退出码 2。
- `pprof` / `metrics` 地址与 console、mcp 或彼此相同 → 配置校验失败，退出码 2。
- `ListenAndServe` 失败后必须尝试关闭另一个 server，避免进程留下半开的监听。
- 优雅停机超时只记 WARN 并继续关闭，不阻塞进程退出。
- `serve` 期间 SIGINT/SIGTERM 是正常路径，退出码 0。

## 测试与验证

- `cmd/memosd/main_test.go`：version、无参、未知子命令、`serve --role` 非法值、`chown` 缺参数。
- `internal/app`：用 `:0` 临时端口启动 `all`，请求 `/healthz` 与 `/readyz`，取消 context 后确认 `Run` 在超时内返回；同名地址冲突返回错误；配置 `MetricsAddr` 时 `/metrics` 可访问。
- 本地单元测试；集群部署在第 3 层验证（deploy 模块）。

## 参考来源

- `cmd/memosd/main.go` 与 `internal/platform/version/version.go`（P0 骨架）。
- `docs/ARCHITECTURE.md` 的“部署形态”与“验证环境”。
- `AGENTS.md` 的“HTTP：不用 http.DefaultClient 与 http.ListenAndServe*”。

## 实现记录

See the public version-control history.
