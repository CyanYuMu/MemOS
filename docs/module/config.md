---
module: config
packages:
  - internal/config
status: implemented
---

# config

把环境变量一次性解析成不可变的 `Config`。进程内其他位置不得再调用 `os.Getenv`，所有取值与校验集中在这里，缺少必填项或取值非法时启动即失败。

## 职责与边界

- 负责：环境变量名与默认值的唯一清单、解析（时长/字节数/布尔/租户映射）、启动期校验、脱敏后的配置摘要。
- 不负责：日志器构造（logging）、身份解析（identity）、上游调用（upstream）。`config` 是叶子包，不 import 本项目任何其他包，也不 import `domain`；需要在层间传递的小结构（如租户映射）由 config 自己定义，由调用方转换。

## 依赖

- 只依赖标准库（`os`、`strconv`、`time`、`encoding/json`、`errors` 等）。
- 被 `internal/app` 使用一次，得到的 `Config` 按值或只读指针传给各组件。

## 契约

```go
type Role string
const (
    RoleConsole Role = "console"
    RoleMCP     Role = "mcp"
    RoleAll     Role = "all"
)

func ParseRole(s string) (Role, error)
func Load() (Config, error)          // 读 os.Environ；供 main 使用
func FromEnv(getenv func(string) string) (Config, error) // 测试注入

type Config struct {
    ServiceName string
    Role        Role
    Log         LogConfig
    HTTP        HTTPConfig
    Upstream    UpstreamConfig
    Store       StoreConfig
    Identity    IdentityConfig
    Limits      LimitsConfig
    Worker      WorkerConfig
    Audit       AuditConfig
    Chat        ChatConfig
}
```

字段与来源（`MEMOSD_` 前缀是控制面自有配置，`MEMOS_` 前缀是与既有网关/上游共用的名字）：

| 字段 | 环境变量 | 默认值 | 校验 |
|------|----------|--------|------|
| ServiceName | `MEMOSD_SERVICE_NAME` | `MemoryService` | 非空 |
| Role | `MEMOSD_ROLE` | `all` | console/mcp/all；`serve --role` 优先于它 |
| Log.Level | `MEMOSD_LOG_LEVEL` | `info` | debug/info/warn/error |
| Log.Format | `MEMOSD_LOG_FORMAT` | `json` | json/text |
| Log.File | `MEMOSD_LOG_FILE` | 空 | 非空时在 stdout 之外落盘并轮转；prod 为 `/var/log/memory-console/memory-console.jsonl`，dev 为 `/data/logs/memosd.jsonl`（轮转口径见 deploy） |
| Log.MaxSizeMB | `MEMOSD_LOG_MAX_MB` | `1024` | ≥1；单份日志大小上限（默认 1GB） |
| Log.MaxBackups | `MEMOSD_LOG_MAX_BACKUPS` | `5` | ≥0；保留的历史日志份数，0 表示全部保留（prod 用 0） |
| Log.MaxAgeDays | `MEMOSD_LOG_MAX_AGE_DAYS` | `60` | ≥0；0 表示不按时间清理（prod 用 0） |
| Log.Compress | `MEMOSD_LOG_COMPRESS` | `true` | 轮转后的历史文件是否 gzip |
| HTTP.ConsoleAddr | `MEMOSD_CONSOLE_ADDR` | `:5280` | 非空 |
| HTTP.MCPAddr | `MEMOSD_MCP_ADDR` | `:8002` | 非空 |
| HTTP.PprofAddr | `MEMOSD_PPROF_ADDR` | 空 | 空表示关闭；非空时在独立端口暴露 `/debug/pprof/`，不得与 console/mcp 地址相同 |
| HTTP.MetricsAddr | `MEMOSD_METRICS_ADDR` | 空 | 空表示不监听 `/metrics`；非空时独立端口，不得与 console/mcp/pprof 相同 |
| HTTP.ReadHeaderTimeout | `MEMOSD_READ_HEADER_TIMEOUT` | `10s` | >0 |
| HTTP.ReadTimeout | `MEMOSD_READ_TIMEOUT` | `60s` | >0；只限制请求体读取，不截断 SSE 响应 |
| HTTP.ShutdownTimeout | `MEMOSD_SHUTDOWN_TIMEOUT` | `25s` | >0 |
| HTTP.MaxBodyBytes | `MEMOSD_MAX_BODY_BYTES` | `4MiB` | >0 |
| HTTP.CORSOrigins | `MEMOSD_CORS_ORIGINS` | 空 | 逗号分隔的完整来源；为空表示不放通任何跨域（开发用 Vite 代理时同源），不允许 `*` |
| Upstream.BaseURL | `MEMOS_API_BASE_URL` | `http://127.0.0.1:8000` | 合法 URL，去掉尾部 `/` |
| Upstream.Timeout | `MEMOS_TIMEOUT` | `300s` | ≥1s；搜索 p99 实测可达 157s |
| Upstream.MaxResponseBytes | `MEMOSD_UPSTREAM_MAX_BYTES` | `8MiB` | >0；挡 132MB 的 `metadata.sources` |
| Upstream.DashboardMaxBytes | `MEMOSD_UPSTREAM_DASHBOARD_MAX_BYTES` | `512MiB` | >0；控制台记忆列表流式读取的原始字节上限（实测一页可达 148MB），与通用上限分开 |
| Store.Path | `MEMOSD_DB_PATH` | `data/memosd.db` | 非空 |
| Store.MigrationTimeout | `MEMOSD_MIGRATION_TIMEOUT` | `10m` | >0；启动时执行迁移的时间上限，与后续装配的 30s 预算分开计时。大库跑 `0015` 全表改写可能超过 30s，用装配预算会超时退出并被编排反复重启。迁移结束后的 Bootstrap/账号初始化另起 30s 窗口，不会拿着已过期的 ctx 启动。注意：SIGTERM 仍会终止进程（信号在 `app.New` 之后才注册），未提交的迁移事务由 SQLite 回滚，下次启动重跑 |
| HTTP.WebDir | `MEMOSD_WEB_DIR` | 空 | 控制台前端静态产物目录；非空时必须存在且含 `index.html`，否则启动失败。空表示只提供 API |
| Identity.TenantTokens | `MEMOS_TENANTS` | 空 | 见下方格式；重复 token、空 user_id 报错 |
| Identity.AdminToken | `MEMOSD_ADMIN_TOKEN` | 空 | 空表示不启用 admin 写接口；非空时长度 ≥ 16 |
| Auth.SeedUser | `MEMOSD_ADMIN_USER` | `admin` | 设置了种子口令时长度 1..64 |
| Auth.SeedPassword | `MEMOSD_ADMIN_PASSWORD` | 空 | 空表示不种子；非空时 ≥ 8 字节。只在账号表里没有该用户名时写入，不覆盖既有口令；`Redacted()` 只输出 `seed_admin=true/false` |
| Auth.SessionTTL | `MEMOSD_SESSION_TTL` | `12h` | ≥1m；会话空闲超时，每次请求最多每分钟续期一次 |
| Auth.SessionMaxLifetime | `MEMOSD_SESSION_MAX_LIFETIME` | `720h` | ≥ `MEMOSD_SESSION_TTL`，否则启动失败；会话绝对上限（默认 30 天），也是登录 cookie 的 `Max-Age` |
| Auth.MaxLoginFailures | `MEMOSD_LOGIN_MAX_FAILURES` | `5` | ≥1；同一用户名或来源 IP 在窗口内的失败次数阈值 |
| Auth.LoginFailWindow | `MEMOSD_LOGIN_FAIL_WINDOW` | `10m` | ≥1s；失败计数的滑动窗口 |
| Auth.InviteDefaultTTL | `MEMOSD_INVITE_DEFAULT_TTL` | `168h` | ≥1h 且 ≤ `InviteMaxTTL`；创建邀请未给过期时间时的有效期（7 天） |
| Auth.InviteMaxTTL | `MEMOSD_INVITE_MAX_TTL` | `720h` | ≥1h；创建邀请允许的最长有效期（30 天），超过返回 400，不静默截断 |
| Auth.AllowPassword | `MEMOSD_AUTH_ALLOW_PASSWORD` | `true` | 为 false 时 `/__login` 返回 403；与 `ZEROTRUST_REQUIRED=true` 组合时必须为 false。单独置 false 而不开零信任会失去全部交互登录入口（只剩管理 Token 与既有会话），校验不拦，按应急操作对待 |
| Auth.ZeroTrust.Enabled | `MEMOSD_ZEROTRUST_ENABLED` | `false` | 校验 `X-Zt-Authorization` 并启用外部身份认证 |
| Auth.ZeroTrust.Required | `MEMOSD_ZEROTRUST_REQUIRED` | `false` | 只允许外部身份或管理 Token；本地会话不再单独通过 |
| Auth.ZeroTrust.AutoCreate | `MEMOSD_ZEROTRUST_AUTO_CREATE` | `true` | 首次出现的 username 自动建无口令账号 |
| Auth.ZeroTrust.Secrets | `MEMOSD_ZEROTRUST_SECRET` | 空 | 逗号分隔的 JWT Secret 列表；Enabled 时至少一项且每项 ≥16 字节。轮换期可同时填新旧两个；不进 `Redacted()` |
| Auth.ZeroTrust.AllowedUsers | `MEMOSD_ZEROTRUST_ALLOWED_USERS` | 空 | 逗号分隔 username 白名单；空表示不限制，同一 username 重复报错 |
| Auth.ZeroTrust.LogoutURL | `MEMOSD_ZEROTRUST_LOGOUT_URL` | 空 | 外部身份登出后 303 的地址；空表示只清本地会话 |
| Limits.DefaultTopK | `MEMOSD_DEFAULT_TOP_K` | `10` | 1..50 |
| Limits.MaxTopK | `MEMOSD_MAX_TOP_K` | `25` | 1..25；上游契约 v2 的 `top_k_max`，超过会让检索请求 422 |
| Limits.DreamTopK | `MEMOSD_DREAM_TOP_K` | `2` | ≥0；0 表示 search 不召回 dream 桶 |
| Limits.GraphMaxNodes | `MEMOSD_GRAPH_MAX_NODES` | `4000` | 1..10000；控制台图谱单次返回的节点上限，也是 `limit` 参数的上限（上游 schema 上限） |
| Limits.QueryPrefix | `MEMORY_QUERY_PREFIX` | 空 | 叠加在 search query 前，空表示不加 |
| Prompt.TestBurst | `MEMOSD_PROMPT_TEST_BURST` | `3` | ≥1；试跑的突发额度（进程内令牌桶，按项目与账号各一份） |
| Prompt.TestRefillPerMin | `MEMOSD_PROMPT_TEST_REFILL_PER_MIN` | `6` | ≥1；试跑每分钟补充的令牌数 |
| Prompt.GenerateBurst | `MEMOSD_PROMPT_GENERATE_BURST` | `2` | ≥1；AI 改写的突发额度 |
| Prompt.GenerateRefillPerMin | `MEMOSD_PROMPT_GENERATE_REFILL_PER_MIN` | `4` | ≥1；AI 改写每分钟补充的令牌数 |
| Worker.AddAsync | `MEMOS_ADD_ASYNC` | `true` | 布尔词表 |
| Worker.PollInterval | `MEMOSD_WORKER_POLL` | `2s` | >0 |
| Worker.Concurrency | `MEMOSD_WORKER_CONCURRENCY` | `4` | ≥1；同时消费的任务数，同一租户始终串行 |
| Worker.JobTimeout | `MEMOSD_JOB_TIMEOUT` | `5m` | >0；同时用于 worker 执行上限与停机排空上限 |
| Worker.MaxAttempts | `MEMOSD_JOB_MAX_ATTEMPTS` | `3` | ≥1；异步写入失败后的最大尝试次数 |
| Worker.RetryBase | `MEMOSD_JOB_RETRY_BASE` | `5s` | >0；首次重试等待，之后按 2 的幂递增并叠加 0–50% 抖动 |
| Worker.RetryMax | `MEMOSD_JOB_RETRY_MAX` | `10m` | ≥RetryBase；单次重试等待上限 |
| Worker.RetentionDays | `MEMOSD_JOB_RETENTION_DAYS` | `7` | ≥1；succeeded/failed 任务的保留天数，worker 每小时清理一次 |
| Worker.IdempotencyTTL | `MEMOSD_IDEMPOTENCY_TTL` | `24h` | ≥1m；请求去重记录的保留时长 |
| Audit.RetentionDays | `MEMOSD_AUDIT_RETENTION_DAYS` | `30` | ≥1；SQLite 查询副本的保留天数，必须显式给正数（完整原文以 `Audit.File` 为准，不受该值影响） |
| Audit.QueueSize | `MEMOSD_AUDIT_QUEUE_SIZE` | `1024` | ≥1；异步落库队列长度，满时丢弃并记 warn |
| Audit.FlushInterval | `MEMOSD_AUDIT_FLUSH_INTERVAL` | `1s` | ≥100ms；批量落库周期 |
| Audit.StatsInterval | `MEMOSD_AUDIT_STATS_INTERVAL` | `30s` | ≥1s；队列统计摘要与缺口汇总行的汇报周期 |
| Audit.File | `MEMOSD_AUDIT_FILE` | 空 | 非空时追加完整 JSONL 归档；prod 为 `/var/log/memory-console/audit.jsonl` |
| Audit.FileMaxSizeMB | `MEMOSD_AUDIT_FILE_MAX_MB` | `1024` | 单份文件大小；0 取 1024 |
| Audit.FileMaxBackups | `MEMOSD_AUDIT_FILE_MAX_BACKUPS` | `0` | 0 表示保留全部轮转文件 |
| Audit.FileMaxAgeDays | `MEMOSD_AUDIT_FILE_MAX_AGE_DAYS` | `0` | 0 表示不按天数删除 |
| Audit.FileCompress | `MEMOSD_AUDIT_FILE_COMPRESS` | `true` | 轮转文件是否 gzip |
| Redis.URL | `MEMOSD_REDIS_URL` | 空 | 空表示节流只用进程内存；非空须是 `redis://` 或 `rediss://` 且带 host，可含 userinfo（摘要里会去掉） |
| Redis.Prefix | `MEMOSD_REDIS_PREFIX` | `memosd:` | 非空；节流键前缀，避免与同一 Redis 上的调度器等业务冲突 |
| Redis.Timeout | `MEMOSD_REDIS_TIMEOUT` | `500ms` | >0；拨号与读写超时 |
| Chat.Dir | `MEMOSD_CHAT_DIR` | 空 | 聊天会话根目录；空表示本进程不挂载 `/api/chat/*`，其余 Chat 配置不参与装配 |
| Chat.PiBin | `MEMOSD_CHAT_PI_BIN` | `pi` | 非空；pi 可执行文件路径，裸命令走 PATH |
| Chat.MaxProcs | `MEMOSD_CHAT_MAX_PROCS` | `12` | ≥1；全局在途 agent 进程上限，超限返回 429。12×45MB≈540MB，给 Pod limit 2Gi 留余量 |
| Chat.IdleTTL | `MEMOSD_CHAT_IDLE_TTL` | `15m` | >0；无订阅者且无在途请求的进程空闲回收时间，回收检查周期 1m |
| Chat.MCPURL | `MEMOSD_CHAT_MCP_URL` | `http://127.0.0.1:8002/mcp` | 带 host 的 http(s) URL；写进 pi 的 `mcp.json`，同 Pod 内走 localhost |
| Chat.ContextWindow | `MEMOSD_CHAT_CONTEXT_WINDOW` | `131072` | ≥1；写进 `models.json` 的上下文窗口 |
| Chat.MaxTokens | `MEMOSD_CHAT_MAX_TOKENS` | `16384` | ≥1；写进 `models.json` 的单次输出上限 |
| LocalSync.Enabled | `MEMOSD_LOCAL_SYNC_ENABLED` | `false` | 布尔词表；关闭时 `/api/local-sync/*` 与本地同步 Dashboard 路由统一 503。默认关闭是刻意的：插件端尚未发版，打开即暴露一组会把记忆正文写入控制面库的接口 |
| LocalSync.VerificationURI | `MEMOSD_LOCAL_SYNC_VERIFICATION_URI` | 空 | 空表示由请求 Host 推导设备授权页地址；非空须是带 host 的 http(s) 绝对 URL |
| LocalSync.DeviceCodeTTL | `MEMOSD_LOCAL_SYNC_DEVICE_CODE_TTL` | `10m` | ≥1m；设备授权码有效期 |
| LocalSync.PollInterval | `MEMOSD_LOCAL_SYNC_POLL_INTERVAL` | `5s` | ≥1s；插件轮询换取设备令牌的建议间隔 |
| LocalSync.TokenTTL | `MEMOSD_LOCAL_SYNC_TOKEN_TTL` | `4320h` | ≥24h；设备令牌滑动过期窗口，`last_seen_at` 每次请求刷新（节流 1 分钟） |
| LocalSync.BatchTTL | `MEMOSD_LOCAL_SYNC_BATCH_TTL` | `24h` | ≥1h；未提交批次的分块保留期，超期不可续传 |
| LocalSync.MaxChunkBytes | `MEMOSD_LOCAL_SYNC_MAX_CHUNK_BYTES` | `4MiB` | >0 且 ≤ `MaxBatchBytes`；console 角色下还须 ≤ `MEMOSD_MAX_BODY_BYTES`（分块要过统一请求体中间件） |
| LocalSync.MaxBatchBytes | `MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES` | `64MiB` | >0；单批 gzip 后的累计字节上限 |
| LocalSync.MaxBatchDecompressed | `MEMOSD_LOCAL_SYNC_MAX_BATCH_DECOMPRESSED` | `256MiB` | ≥ `MaxBatchBytes`；一次 commit 全部块解压后的总量上限，挡压缩炸弹与内存放大。未显式设置时跟随 `MaxBatchBytes`，避免存量部署调大单批上限后启动失败 |
| LocalSync.MaxRowBytes | `MEMOSD_LOCAL_SYNC_MAX_ROW_BYTES` | `256KiB` | >0；单行 payload 序列化后的字节上限 |
| LocalSync.QuotaBytes | `MEMOSD_LOCAL_SYNC_QUOTA_BYTES` | `2GiB` | >0；单项目镜像存储上限（含未提交分块的 pending 占用） |
| LocalSync.QuotaRows | `MEMOSD_LOCAL_SYNC_QUOTA_ROWS` | `500000` | >0；单项目镜像行数上限（软删除行立即释放配额） |
| LocalSync.RatePerMinute | `MEMOSD_LOCAL_SYNC_RATE_PER_MINUTE` | `120` | >0；设备维度的令牌桶速率，作用于建批次、传分块与 commit |
| LocalSync.DeletedRetention | `MEMOSD_LOCAL_SYNC_DELETED_RETENTION` | `168h` | ≥0；软删除行的物理清理期限，0 表示不清理。清理前写墓碑，防止同内容重传复活 |
| LocalSync.MirrorRetention | `MEMOSD_LOCAL_SYNC_MIRROR_RETENTION` | `0` | ≥0；镜像保留期，0 表示永久保留；只影响控制面副本，不碰本地与云端记忆 |
| LocalSync.TombstoneRetention | `MEMOSD_LOCAL_SYNC_TOMBSTONE_RETENTION` | `4320h` | ≥0；删除墓碑保留期，0 表示永不清理 |

`MEMOS_TENANTS` 支持两种写法（与既有网关 `gateway/domain/tenants.py` 一致）：

- JSON：`{"tok-a":{"user_id":"alice","cube_id":"alice-cube"}}`，`cube_id` 缺省等于 `user_id`；
- 紧凑：`tok-a:alice,tok-b:bob`，token 里可以含冒号（按最后一个冒号切分），`user_id` 不允许含冒号。

解析结果：

```go
type TenantToken struct {
    Token  string
    UserID string
    CubeID string
}
```

不变量：

- 解析只发生一次；`Config` 构造后不可变，不持有 `os.Environ` 引用。
- 错误信息可以报环境变量名与“非法”，**不得回显取值**（`MEMOS_TENANTS`、`MEMOSD_ADMIN_TOKEN` 是凭据）。
- `String()` / 日志摘要只输出 token 数量、管理员是否启用、地址、超时等，不输出任何 token 值。

## 错误与边界情况

- 未设置的变量一律取默认值，不因为“缺配置”静默连到错误目标：上游默认指向 `127.0.0.1`，租户默认为空（此时所有 Bearer 都会 401），管理员 Token 默认关闭。
- 数值解析失败（如 `MEMOS_TIMEOUT=abc`）→ 返回 `error`，进程退出码 2，不退回默认值。
- `MEMOSD_CORS_ORIGINS` 含 `*` → 报错。通配 CORS 会让任意站点读取带凭据的响应，这里在启动期拦住。
- 布尔接受 `1/0/true/false/yes/no/on/off`（大小写不敏感），其他值报错。

## 测试与验证

- 表驱动：默认值、完整覆盖、非法数值、非法角色、`*` CORS、租户两种写法、重复 token、空 user_id、`MEMOSD_ADMIN_TOKEN` 过短、种子口令过短、种子用户名过长、会话 TTL 过短、`MEMOSD_SESSION_MAX_LIFETIME` 小于 `MEMOSD_SESSION_TTL`、`MEMOSD_COOKIE_SECURE` 非法值、邀请默认有效期大于上限、Redis URL 的 scheme/host 与超时，以及 Auth/Redis/LocalSync 默认值与 LocalSync 的 TTL 下限、块/批配额包含关系；摘要不含 Redis 密码。
- 脱敏：`Config.Redacted()` 的输出里不出现任何 token 值与种子口令。
- 全部是本地单元测试，不涉及集群。

## 参考来源

- `AGENTS.md` 的“配置只在 internal/config 读环境变量一次”。

## 实现记录

See the public version-control history.
## 2026-11-01 对外接入地址（implemented）

`MEMOSD_PUBLIC_SERVICE_URL`/`MEMOSD_PUBLIC_CONSOLE_URL` 归HTTP配置管理，均空为未启用；配置时必须成对、为不含凭据/query/fragment的HTTP(S)绝对地址，去掉尾斜杠。HTTP构造提取为同组helper以保持入口长度。只供运行时前端脚本及安装脚本使用，不从分支名推导，不影响内部数据面地址或鉴权。
