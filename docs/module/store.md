---
module: store
packages:
  - internal/adapter/store
  - internal/service
status: implemented
---

# store

控制面自有数据的持久化：租户、Token 哈希、审计、异步任务。端口接口在 `internal/service` 单方面声明，`internal/adapter/store` 用 SQLite 实现；adapter 不 import service（depguard 强制），靠方法签名结构化满足接口。

## 职责与边界

- 负责：`modernc.org/sqlite`（纯 Go，`CGO_ENABLED=0`）连接、内嵌迁移、CRUD、事务边界、`Ping`。
- 不负责：身份校验与 Token 签发（identity）、审计的异步队列与重试（audit）、业务用例（memory）。不缓存，不做连接池外的全局状态。
- 换 PG 的余地：SQL 只用 SQLite 与 PG 的公共子集（`CREATE TABLE`、`INSERT ... ON CONFLICT`、`SELECT`、`UPDATE`、`DELETE`），不用 AUTOINCREMENT、不用 `INSERT OR REPLACE`；ID 由 Go 生成；占位符是唯一方言差异，集中在 store 内。

## 依赖

- 依赖 `internal/domain` 与标准库 `database/sql`、`embed`；迁移文件经根目录 `migrations` 包嵌入。
- `internal/service` 声明端口，不依赖 adapter；`internal/app` 把 `*store.Store` 传给 service。

## 契约

### 端口

`internal/service/ports.go` 只声明跨模块共用的聚合端口；更窄的端口由各使用方在自己的包里声明，`*store.Store` 靠方法签名结构化满足。换 PG 时只需保证这些方法齐全。

```go
// internal/service/ports.go
type TenantRepo interface {
    GetTenant(ctx context.Context, id string) (domain.Tenant, error)
    UpsertTenant(ctx context.Context, t domain.Tenant) error
    ListTenants(ctx context.Context) ([]domain.Tenant, error)
}

type TokenRepo interface {
    CreateToken(ctx context.Context, t domain.Token) error
    UpsertToken(ctx context.Context, t domain.Token) error
    TokenByHash(ctx context.Context, hash [32]byte) (domain.Token, error)
    GetToken(ctx context.Context, id string) (domain.Token, error)
    ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
    RevokeToken(ctx context.Context, id string, at time.Time) error
    TouchToken(ctx context.Context, id string, at time.Time) error
}

type AuditRepo interface {
    InsertAudit(ctx context.Context, r domain.AuditRecord) error
    InsertAudits(ctx context.Context, records []domain.AuditRecord) error
    ListAudit(ctx context.Context, f AuditFilter) ([]domain.AuditRecord, int, error)
    GetAudit(ctx context.Context, id string) (domain.AuditRecord, error)
    AuditHourCounts(ctx context.Context, f AuditFilter) ([]domain.AuditHourCount, error)
    PurgeAuditBefore(ctx context.Context, before time.Time) (int64, error)
}

type JobRepo interface {
    EnqueueJob(ctx context.Context, j domain.Job) error
    ClaimJob(ctx context.Context, now time.Time) (domain.Job, bool, error)
    ResetStaleJobs(ctx context.Context, now, staleBefore time.Time) (int64, error)
    RescheduleJob(ctx context.Context, p domain.JobReschedule) error
    FinishJob(ctx context.Context, p domain.JobCompletion) error
}

type Repository interface {
    TenantRepo
    TokenRepo
    AuditRepo
    JobRepo
    Ping(ctx context.Context) error
    Close() error
}
```

使用方另外还会直接用到（不在 `Repository` 聚合里，由各自端口声明）：

- 账号/邀请/会话：`CreateAccount`、`GetAccount`、`AccountByUsername`、`ListAccounts`、`ListAccountsPage`、`UpdateAccount`、`UpdateAccountAndRevokeSessions`、`SetAccountProfile`、`SetAccountTenant`、`TouchLogin`、`CountEnabledPlatformAdmins`、`DeleteAccount`、`PutMember`、`RemoveMember`、`ListMembers`、`MembershipsOf`、`SearchAccounts`、`CreateSession`、`SessionByHash`、`GetSession`、`SetSessionTenant`、`ExtendSession`、`RevokeSession`、`RevokeAccountSessions`、`RevokeAccountSessionsExcept`、`ListSessions`、`PurgeSessionsBefore`、`PersonalTenantOf`、`EnsurePersonalTenant`、`CreateInvite`、`GetInviteByHash`、`GetInvite`、`ListInvites`、`RevokeInvite`、`UpdateInviteRole`、`RedeemInvite`、`PurgeInvitesBefore`。
- 租户生命周期：`CreateTenant`、`CreateTenantWithOwner`、`EnsureTenant`、`EnsurePersonalTenant`、`ArchiveTenant`、`RestoreTenant`。
- Token 改名：`UpdateTokenLabel`（`identity.RenameToken` 用）。
- 任务运维：`ListJobs`、`GetJob`、`RequeueJob`、`JobStats`、`PurgeFinishedJobsBefore`。
- 审计导入与维护：`AuditIDsExist`（旧 JSONL 去重）、`Optimize`（`PRAGMA optimize`）。
- 请求去重：`ClaimIdempotency`、`CompleteIdempotency`、`AcceptIdempotency`、`SucceedIdempotency`、`ReleaseIdempotency`、`PurgeIdempotencyBefore`。
- 连接池观测：`DBStats()` 返回写/读两个 `sql.DBStats`。
- 聊天配置：`GetChatConfig`、`UpsertChatConfig`（迁移 `0027`）。

### 适配器

```go
func Open(ctx context.Context, path string) (*Store, error) // 建目录、打开、设置 PRAGMA、跑迁移
func (s *Store) Ping(ctx context.Context) error
func (s *Store) Close() error
func (s *Store) Optimize(ctx context.Context) error // 批量清理后执行 PRAGMA optimize
func (s *Store) DBStats() (write, read sql.DBStats)
```

- 打开参数：`_pragma=busy_timeout(5000)`、`_pragma=journal_mode(WAL)`、`_pragma=foreign_keys(1)`、`_pragma=synchronous(NORMAL)`。
- 连接：写连接 `SetMaxOpenConns(1)`（SQLite 同一时刻只有一个写者）；文件库另开一个只读连接池（`query_only(1)`，8 条连接）承担鉴权、审计查询、列表等读请求。WAL 下读不阻塞写，读请求不再排在写事务后面。`:memory:` 测试库共用同一个单连接。
- 一致性：读池每次查询都是新快照，已提交的写可见；同一请求内没有“先写后立刻用读池校验”的路径。
- 迁移：`migrations/*.sql` 按文件名升序执行，记录在 `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL DEFAULT '')`；每个文件一个事务，失败回滚并返回错误。已应用文件的 SHA-256 与库中记录不一致时拒绝启动；空校验和是引入校验前的历史行，首轮自动回填。
- 迁移号只增不复用：文件发布后内容不可再改（校验和不一致直接拒绝启动），新增改动只能追加更高版本的文件。
- 时间统一存**固定宽度毫秒 UTC** 文本：`encodeTime` 用 `2006-01-02T15:04:05.000Z07:00`，UTC 下输出 `2026-10-23T00:00:00.000Z`（时间本体 23 字符，加 `Z` 共 24 字符）；`decodeTime` 用同一 layout 严格解析，非固定宽度文本一律报错。固定宽度保证“字符串字典序 = 时间序”，`since`/`until`、purge、`ClaimJob` 与会话过期比较才可靠；历史的变长格式（整秒无小数、纳秒）由迁移 `0015_timestamp_format.sql` 一次性改写。布尔存 `INTEGER 0/1`。
- 维护：`PurgeAuditBefore` 用 `DELETE ... WHERE id IN (SELECT id ... LIMIT 5000)` 分批删除，每批自动提交后再尽力 `PRAGMA wal_checkpoint(PASSIVE)`；ctx 取消时返回已删除数量与错误。`Optimize` 执行 `PRAGMA optimize`，由 audit 清理循环在一轮删除结束后调用一次，避免每批都做分析。

### 审计查询语义

- `ListAudit` 按 `ts DESC, id DESC` 返回，`total` 是忽略分页后的总数；只取元数据，`input`/`output`/`query` 用空串占位，正文只在 `GetAudit` 返回。单次 `limit` 缺省 100、上限 500。
- 过滤条件：`TenantID`、`Tool`、`Tools`、`ExcludeTools`、`Sources`（`source IN (...)`，历史空 source 不会被任何来源命中）、`OK`（行内）、`Status`（按 `call_id` 合并终态行后的派生状态）、时间区间（含 `since`、不含 `until`）。
- `Text` 在 `input_json`/`output`/`query`/`err`/`err_message`/`session_id`/`tenant_id` 上做不区分大小写的子串匹配（`%`/`_` 按字面处理），并额外用相关子查询命中同一 `call_id` 终态行的 `err`/`output`，与 `GetAudit` 的合并口径一致。
- `auditStatusExpr` 的派生状态全部在 SQL 内计算：非 deferred 行按自身 `ok`；deferred 行优先取同一 `call_id` 的最新终态行；没有终态行时只有 `jobs` 里仍有同 id 任务才算 `RUNNING`，job 被清理后回退到行内 `ok`。状态过滤、计数与分页都复用它。
- `GetAudit` 对 deferred 请求行合并终态行的 `err`/`err_message`/`http_status`/`output` 与 token 用量；入参、查询词与 `ResultBytes` 仍取请求行。不存在返回 `ErrNotFound`。
- `AuditHourCounts` 按 `substr(ts,1,13)`（UTC 整点）与工具分组，返回调用数与 `request_tokens`/`response_tokens`/`llm_calls` 合计；无法解析的历史时间桶跳过，不让脏数据把用量接口打成 500。

### 表

```sql
tenants(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', multi_view INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'team', created_by_account_id TEXT NOT NULL DEFAULT '',
        archived_at TEXT NOT NULL DEFAULT '')
tokens(id TEXT PRIMARY KEY, kind TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
       user_id TEXT NOT NULL DEFAULT '', hash BLOB NOT NULL UNIQUE, prefix TEXT NOT NULL,
       raw TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
       last_used_at TEXT, revoked_at TEXT, created_by_account_id TEXT NOT NULL DEFAULT '',
       expires_at TEXT NOT NULL DEFAULT '')
audit_records(id TEXT PRIMARY KEY, call_id TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL,
              tool TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
              tenant_id TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
              session_id TEXT NOT NULL DEFAULT '', case_like INTEGER, deferred INTEGER NOT NULL DEFAULT 0,
              ms INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL, err TEXT NOT NULL DEFAULT '',
              err_message TEXT NOT NULL DEFAULT '', http_status INTEGER NOT NULL DEFAULT 0,
              result_bytes INTEGER NOT NULL DEFAULT 0, input_json TEXT NOT NULL DEFAULT '{}',
              output TEXT NOT NULL DEFAULT '', query TEXT NOT NULL DEFAULT '',
              request_id TEXT NOT NULL DEFAULT '', remote_ip TEXT NOT NULL DEFAULT '',
              request_tokens INTEGER NOT NULL DEFAULT 0, response_tokens INTEGER NOT NULL DEFAULT 0,
              llm_calls INTEGER NOT NULL DEFAULT 0)
accounts(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL DEFAULT '',
         email TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
         platform_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
         current_tenant_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, last_login_at TEXT)
project_members(account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE, tenant_id TEXT REFERENCES tenants(id),
                role TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (account_id, tenant_id))
sessions(id TEXT PRIMARY KEY, hash BLOB NOT NULL UNIQUE, account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
         current_tenant_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
         last_seen_at TEXT, revoked_at TEXT)
jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
     user_id TEXT NOT NULL DEFAULT '', payload BLOB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
     last_error TEXT NOT NULL DEFAULT '', next_attempt_at TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
idempotency_keys(tenant_id TEXT NOT NULL, idem_key TEXT NOT NULL, tool TEXT NOT NULL,
                 request_hash TEXT NOT NULL, state TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                 PRIMARY KEY (tenant_id, idem_key))
project_invites(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
                role TEXT NOT NULL, token_hash BLOB NOT NULL UNIQUE,
                created_by_account_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, used_at TEXT, used_by_account_id TEXT NOT NULL DEFAULT '',
                revoked_at TEXT)
chat_configs(tenant_id TEXT PRIMARY KEY, base_url TEXT NOT NULL DEFAULT '',
             model TEXT NOT NULL DEFAULT '', api_key TEXT NOT NULL DEFAULT '',
             updated_at TEXT NOT NULL)
```

索引：`tokens(hash)`（UNIQUE）、`tokens(tenant_id)`、`audit_records(ts)`、`audit_records(tenant_id, ts)`、`audit_records(tenant_id, tool, ts)`、`audit_records(tool, ts)`、`audit_records(call_id, tool)`、`audit_records(tenant_id, ts, id)`、`audit_records(tool, ts, id)`、
`idempotency_keys(updated_at)`、`jobs(status, created_at)`、`jobs(status, updated_at)`、`jobs(status, tenant_id)`、`jobs(status, next_attempt_at)`、`jobs(status, next_attempt_at, created_at)`、`jobs(tenant_id, created_at)`、`project_members(tenant_id)`、`sessions(account_id)`、
`sessions(expires_at)`、`sessions(account_id, revoked_at, expires_at)`、`accounts(created_at, id)`、
`tenants(user_id) WHERE user_id <> ''`（UNIQUE 部分索引；`user_id` 映射上游 `user_name`，非空值不能重复）、
`tenants(created_by_account_id) WHERE kind='personal'`（UNIQUE 部分索引，每人至多一个个人项目）、
`project_invites(tenant_id, created_at)`、`project_invites(expires_at)`。

`tenants.kind` 只有 `team`（默认，含历史行）与 `personal` 两种；`created_by_account_id` 记录创建者账号，个人项目靠它做唯一约束与归属回查。个人项目的 `id`/`user_id` 都是 `u-<accountID>`，所以上游节点上的 `user_name` 天然按账号隔离，不会与人工起的项目名或上游已存在的 `user_id` 撞车。`archived_at` 非空表示归档（软删除），归档同时置 `disabled=1`，恢复时两者一起清空。

### 时间格式与迁移

- 迁移 `0015_timestamp_format.sql` 把 `tenants.created_at`、`tokens`、`audit_records`、`jobs`、`accounts`、`project_members`、`sessions`、`idempotency_keys`、`project_invites` 与 `schema_migrations.applied_at` 的既有时间文本统一改写为 `strftime('%Y-%m-%dT%H:%M:%fZ', col)` 的输出；空串与 `NULL` 原样保留。迁移在一个事务里执行，失败则整个文件回滚、`Open` 返回错误、进程不启动。
- 精度：SQLite 的 `%f` 是毫秒，历史纳秒部分被截断（例如 `.123456789Z` 变为 `.123Z`），与新 `encodeTime` 的格式一致。需要亚毫秒精度的场景本库不提供。
- 空值约定：**“没有值”一律用空串**，不用 `NULL`。`optionalTimeText` 写空串、`decodeOptionalTime` 把空串读成 `nil`，查询只写 `x = ''`。同列混用 `NULL` 与空串时，每个查询都得写 `(x IS NULL OR x = '')`，漏一半就是静默少行；迁移 `0025` 已把存量 `NULL` 归一。唯一的例外是审计的 `case_like`：它是有第三态的布尔（`NULL` = 这条记录不参与该检查），不是“没有值”。
- 解析：`decodeTime` 只接受上述固定宽度格式（`0015` 已把历史行全部改写），读到变长文本直接报错——宁可让一行脏数据暴露出来，也不要让“字符串序 = 时间序”的前提悄悄失效。任何依赖字符串比较的查询（`since`/`until`、purge、`ClaimJob`、会话过期）都要求列已是固定宽度，`Open` 在接流量前跑完迁移满足这一点。
- 迁移 `0016_indexes.sql` 建立 `idx_audit_tool_ts`（已被 `0025` 删除，见下）、`idx_jobs_claim` 与 `idx_tenants_user_id`。其中 `idx_tenants_user_id` 是 `WHERE user_id <> ''` 的部分唯一索引：空串（历史/占位行）不参与约束，非空 `user_id` 不允许重复。
- `0019` 给审计加 `err_message`/`http_status`，`0020` 补列表分页索引，`0021` 给租户加 `archived_at`，`0022` 加 token 用量三列，`0023` 加 `source`，`0024` 加 `tokens.raw`，`0026` 加审计客户端信息，`0027` 建 `chat_configs`。这些都是加法迁移，旧版本二进制不读新列即可回滚，但回滚后复制明文、归档、来源筛选与聊天不可用。
- **运维提示（重复 user_id）**：如果历史数据里已经有两个项目指向同一个非空 `user_id`，`0016` 会失败，`store.Open` 返回错误、进程拒绝启动。这是有意的“失败要响”，因为两个项目共享上游 `user_name` 会互相串记忆数据。处理方式：先停服/停写，人工确认哪个项目应保留该 `user_id`，把另一个项目的 `user_id` 改成新的上游用户名（或合并项目），再启动；**不要**删除或弱化该索引来绕过启动失败。

### 请求去重记录

`idempotency_keys` 的状态由 `memory` 服务驱动，store 只做带前置状态的条件更新：

| 方法 | 转换 | 用途 |
|------|------|------|
| `ClaimIdempotency` | （无）→ `in_progress` | 首次占用；已存在时返回旧记录，由调用方判断重放/冲突 |
| `CompleteIdempotency` | `in_progress` → `succeeded`，写响应 | 同步写入成功 |
| `AcceptIdempotency` | `in_progress` → `accepted`，写“已接收”响应 | 异步入队成功 |
| `SucceedIdempotency` | `accepted` → `succeeded`，保留响应 | worker 终态成功 |
| `ReleaseIdempotency` | 删除 `in_progress`/`accepted` | 请求失败或 worker 永久失败，允许重试 |
| `PurgeIdempotencyBefore` | 删除超期行 | worker 按 `IdempotencyTTL` 清理 |

- 条件更新命中 0 行返回 `not_found`，避免并发下把别人的记录改掉。
- `response` 存首次响应的 JSON，`succeeded`/`accepted` 的重发直接重放，不再触发上游写入。

### 数据归属与保留

- 本库只存控制面数据：租户投影、Key 的哈希/明文副本/元数据、账号/成员/会话、审计、异步任务。记忆正文、画像、知识库与画像模板都在上游 memos-api，本库不复制。
- 保留期：审计 `MEMOSD_AUDIT_RETENTION_DAYS`（默认 30 天，audit 服务每小时清理）；终态任务 `MEMOSD_JOB_RETENTION_DAYS`（默认 7 天，worker 每小时清理）；过期会话在登录时与启动时清理（保留 24h 宽限）；过期超过 30 天的邀请在 `Bootstrap` 时清理（`PurgeInvitesBefore`，只删 `expires_at < cutoff`，活跃与已用记录不受影响）。


不变量：

- `tokens.hash` 是 32 字节 SHA-256，唯一，鉴权查找只认它；`tokens.raw` 是可复制的明文副本（迁移 `0024`），历史行与旧网关导入的 Key 为空，不能把空串当明文下发。`expires_at` 是 UTC 文本，空串表示永不过期；`UpsertToken` 的 `ON CONFLICT` 不更新该列，静态 Token 引导或 ID 迁移都不会清掉或复活已有过期时间；`raw` 只在 `excluded.raw <> ''` 时覆盖，旧行不会被空值抹掉明文。
- `project_invites.token_hash` 同样是 32 字节 SHA-256，唯一；邀请明文只在创建响应出现一次，不落库、不写审计。`used_at`/`revoked_at`/`expires_at` 都是 UTC 文本，状态判定在 domain 里做，SQL 只按条件抢占。
- `tenants` 的部分唯一索引只约束 `kind='personal'`；普通项目允许同名创建者。个人项目的 `disabled` 只能由平台管理员改，store 不做额外校验（服务层是不变量的归属地）。
- `audit_records.input_json` / `output` / `query` 由 audit 服务写入：不脱敏，但单条总量超过 256 KiB 时已由服务层按 UTF-8 边界截断并留下 `audit payload truncated` 标记；store 不再做二次处理。因此列表查询仍只取元数据，正文走 `GetAudit`。`source` 由入口 transport 写入，异步 add 的终态行为空；`err_message`/`http_status`/token 用量是可诊断字段，历史行为默认值。
- `ClaimJob` 只认领 `next_attempt_at` 不晚于 `now` 的最早 `pending` 并 `attempts+1`；已有 `running` 任务的租户会被跳过，因此同一租户串行、不同租户可并发。没有可认领任务返回 `(zero, false, nil)`。每次认领不再附带写事务。`next_attempt_at` 为空串（迁移前的老行）视为立即到期。
- `RescheduleJob` 把失败的 `running` 任务改回 `pending`，写 `last_error` 与 `next_attempt_at`；行、`attempts`、`created_at` 不变。退避期间该任务不阻塞同租户的其它任务。
- `RequeueJob` 是人工重放死信：只接受 `failed`，在一个事务里读状态并条件更新为 `pending`，`attempts` 归零、`last_error` 清空、`next_attempt_at` 置为当前时间；并发重放时后到的 UPDATE 命中 0 行，返回冲突而不是谎报成功。`pending`/`running`/`succeeded` 一律拒绝。
- `ResetStaleJobs` 把 `updated_at` 早于 `staleBefore` 的 `running` 重置为 `pending`（`attempts` 保留，`next_attempt_at` 置为 `now` 以便立即重试），由 worker 低频调用，用于回收进程崩溃遗留的任务。
- `ListJobs` 是死信排查用的只读分页查询，按 `status`/`kind`/`tenant_id` 过滤，按 `created_at` 倒序返回；**不读取 payload**，避免把记忆正文带到运维响应里。`GetJob` 同样不返回 payload。`JobStats` 返回 pending/running/failed 三段计数与最老 pending 的时间。
- `ArchiveTenant` 在 `archived_at = ''` 时写入归档时间并置 `disabled=1`，重复归档保留第一次的时间；`RestoreTenant` 清空 `archived_at` 并恢复启用，重复恢复幂等。成员、邀请与 API Key 都不删除。
- `InsertAudits` 在一个事务里批量写入审计记录；`InsertAudit` 是单条包装。`ListAudit` 的 `COUNT` 与分页查询并行发出，各占一条读连接。

## 错误与边界情况

- 唯一约束冲突（重复 token 哈希）映射为 `domain.ErrConflict`；查不到映射为 `domain.ErrNotFound`；其余 SQL 错误包装为 `ErrUpstreamUnavailable` 之外的内部错误（`Wrap(ErrInvalidInput...)` 不合适，用 `fmt.Errorf` 保留细节，由调用方记日志）。
- `tenants` 的两类唯一冲突可辨认：`id` 重复返回 `ErrConflict`“已存在同名项目”；非空 `user_id` 被其他项目占用返回 `ErrConflict`“该上游 user_id 已被其他项目占用”。`CreateTenant`、`UpsertTenant`、`EnsureTenant` 都做同样的区分，管理端据此返回 409 而不是 500。
- 迁移失败直接返回错误，进程不启动。
- `Close` 可重复调用。
- 审计表按天增长，清理由 audit 服务调用 `PurgeAuditBefore`：按 5000 行分批提交，避免一次 DELETE 几十万行造成长事务与 WAL 暴涨；已取消的 ctx 会中断循环，剩余行留给下一轮。删除后 `Optimize` 刷新统计，`wal_checkpoint(PASSIVE)` 在每批之后尽力回收 WAL。终态任务由 worker 调用 `PurgeFinishedJobsBefore`；请求去重记录由 worker 按 TTL 调用 `PurgeIdempotencyBefore`。
- SQLite 用 WAL，库文件旁会出现 `-wal`/`-shm`；备份必须连 WAL 一起做，进程运行中用 `VACUUM INTO` 得到一致性快照。

## 测试与验证

- 用 `t.TempDir()` 下的 SQLite 文件跑：迁移幂等、存量库升级（时间列改写、`kind` 回填、`tokens.expires_at`、`archived_at`、`source`、`tokens.raw` 等加法迁移）、迁移校验和不一致时拒绝启动、租户 upsert、Token 哈希查找/吊销/改名/明文保留、审计分页/过滤（含 `source`、派生状态、正文合并）/清理、任务 claim 并发只取一次、`RequeueJob` 只接受 failed 且并发只成功一次、`AuditIDsExist` 去重。审计清理另测：多于一批（5000 行）的数据能循环删完并返回总数、小批次参数确实多次提交、已取消 ctx 返回 `context.Canceled` 且不删数据、`Optimize` 可执行。
- 时间与索引：`encodeTime` 输出固定 24 字符（23 字符时间本体 + `Z`）且 UTC，整秒/毫秒/亚毫秒/非 UTC 四种输入；`decodeTime` 解析旧变长与新的固定宽度格式；`since` 落在整秒边界时，旧的 `...00Z` 边界会漏掉同一秒内带小数的行、固定宽度边界不漏；迁移 `0015` 在旧库上覆盖所有时间列，断言整秒补 `.000`、纳秒截断到毫秒、空串与 `NULL` 原样保留、时间值等价；`tenants.user_id` 唯一索引拒绝重复非空值并返回可辨认的冲突消息，空 `user_id` 允许多行；`ListAccountsPage` 在账号多于一页时过滤、总数、分页与 `created_at ASC, id ASC` 排序正确，通配符按字面处理。
- Token 生命周期：带 `ExpiresAt` 的 Token 经 Create/Get/TokenByHash/List 往返保持原值；`UpsertToken` 不清除已有过期时间与明文；`UpdateTokenLabel` 只改名字；静态 Token 读回 nil 过期时间。
- 会话：`GetSession` 读取；`ExtendSession` 同时更新 `last_seen_at` 与 `expires_at`；已吊销或已过期的会话不会被续期；`RevokeAccountSessionsExcept` 保留当前会话。
- 个人项目、归档与邀请：`EnsurePersonalTenant` 并发调用只产生一行且返回同一 ID；`ArchiveTenant` 幂等并保留首次时间、`RestoreTenant` 幂等；`SearchAccounts` 大小写与空查询；`project_invites` 唯一约束；`RedeemInvite` 并发抢占只有一个成功、失败事务不烧邀请、角色只升不降；`PurgeInvitesBefore` 只删过期行。
- 账号删除：`DeleteAccount` 在同一事务里清理个人项目（含成员、邀请、API Key、待执行任务）、团队项目成员关系与会话，并清空团队 Key/邀请的创建者引用；重复删除返回 404。
- 表驱动 + `t.Parallel()`；每个测试独立临时库。
- 本地单元测试；集群验证覆盖 `/readyz` 的 `store` 与 `upstream` 两项。

## 参考来源

- `docs/ARCHITECTURE.md` 的“存储”与“待决事项”（SQLite + hostPath，SQL 公共子集，事务在 Repository 内部）。
- `AGENTS.md` 的“分层”“租户与安全”“依赖”。
- 既有网关的 token 映射与审计字段：`gateway/domain/tenants.py`、`gateway/audit/tool_log.py`。

## 实现记录

See the public version-control history.
