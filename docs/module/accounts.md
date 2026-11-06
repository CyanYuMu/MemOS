---
module: accounts
packages:
  - internal/service/account
  - internal/transport/auth
status: implemented
---

# accounts

控制台的账号、项目空间成员关系、会话与 ACL。解决“谁能登录控制台、能看到哪些项目空间、能做什么”，被 Dashboard（`/api/*`）与 admin（`/admin/*`）共用。数据面的 API Key 仍由 identity 管，本模块只规定 Key 与项目空间、创建者账号的归属。

status 含义：`draft` 还在写，不得写代码；`accepted` 契约已定，可以开始实现；`implemented` 代码与测试已落地并与本文一致。

## 职责与边界

- 负责：账号（口令用 argon2id）、`project_members` 成员关系与角色、服务端会话（cookie）、登录失败节流、种子管理员、`/__login` `/__logout` `/api/auth/me`、把请求解析为 `domain.Principal`、角色矩阵的判定。
- 不负责：租户 Token 的签发与解析（identity）；数据面的 Bearer 鉴权（MCP/REST 仍只认租户 Token）；控制台前端页面（在 `../memory-console`，本模块只提供接口；登录页由后端自带）；SSO/OAuth、邀请、找回口令。
- 概念：项目空间 = 租户 = `cube_id`；账号 ≠ 项目空间，是 N:M；API Key 属于且仅属于一个项目空间。理由与取舍见 `.goal-task/console-accounts-e2e/design.md`，本文是落地契约。

## 依赖

- `internal/domain`（新增 `Role`、`Account`、`Membership`、`Session`、`Principal`、`Credentials`、`ErrRateLimited`；`Token` 增加 `CreatedByAccountID`）。
- 存储端口由 `internal/service/account` 声明，`internal/adapter/store` 结构化实现：

```go
type Store interface {
    GetTenant(ctx context.Context, id string) (domain.Tenant, error)
    ListTenants(ctx context.Context) ([]domain.Tenant, error)

    CreateAccount(ctx context.Context, a domain.Account) error
    GetAccount(ctx context.Context, id string) (domain.Account, error)
    AccountByUsername(ctx context.Context, username string) (domain.Account, error)
    ListAccounts(ctx context.Context) ([]domain.Account, error)
    UpdateAccount(ctx context.Context, a domain.Account) error // display_name、password_hash、platform_admin、disabled
    UpdateAccountAndRevokeSessions(ctx context.Context, a domain.Account, at time.Time) error // 账号更新与吊销会话同一事务
    TouchLogin(ctx context.Context, id string, at time.Time) error
    CountEnabledPlatformAdmins(ctx context.Context) (int, error)

    PutMember(ctx context.Context, m domain.Membership) error // 幂等 upsert
    RemoveMember(ctx context.Context, accountID, tenantID string) error
    ListMembers(ctx context.Context, tenantID string) ([]domain.Membership, error)
    MembershipsOf(ctx context.Context, accountID string) ([]domain.Membership, error)

    CreateSession(ctx context.Context, s domain.Session) error
    SessionByHash(ctx context.Context, hash [32]byte) (domain.Session, error)
    GetSession(ctx context.Context, id string) (domain.Session, error)
    SetSessionTenant(ctx context.Context, id, tenantID string) error
    ExtendSession(ctx context.Context, id string, lastSeen, expiresAt time.Time) error
    RevokeSession(ctx context.Context, id string, at time.Time) error
    RevokeAccountSessions(ctx context.Context, accountID string, at time.Time) error
    PurgeSessionsBefore(ctx context.Context, before time.Time) (int64, error)
}
```

代码里 `Store` 按资源拆成 `TenantStore` / `AccountStore` / `MemberStore` / `SessionStore` / `InviteStore` 五组再组合，方法集合不变；分组只为读起来清楚、写测试替身时按组挑选，`Service` 仍持有完整的 `Store`（登录、成员变更、删除账号本来就跨资源）。

- 管理 Token 的解析通过窄接口注入（不 import identity）：`AuthenticateAdmin(ctx, raw string) (domain.Caller, error)`。
- 被 `internal/transport/dashboard`、`internal/transport/admin`、`internal/app` 依赖；Dashboard 与 admin 在各自包里声明自己需要的窄接口。

## 契约

### 数据模型（迁移 `migrations/0002_accounts.sql`，只增不改）

```sql
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,          -- 存小写，大小写不敏感唯一
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,            -- argon2id PHC 字符串，不含明文
    platform_admin INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE project_members (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id),
    role TEXT NOT NULL,                     -- viewer | editor | owner
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, tenant_id)
);
CREATE INDEX idx_members_tenant ON project_members(tenant_id);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    hash BLOB NOT NULL UNIQUE,              -- SHA-256(会话 Token)，明文只在 cookie 里
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    current_tenant_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT
);
CREATE INDEX idx_sessions_account ON sessions(account_id);
ALTER TABLE tokens ADD COLUMN created_by_account_id TEXT NOT NULL DEFAULT '';
```

迁移由 `store.migrate()` 按版本号顺序在事务内应用并记入 `schema_migrations`，所以“幂等”指重复启动不会重复应用；已有 `tenants`、`tokens`、`audit_records`、`jobs` 的行为与数据不变。SQL 只用 SQLite 与 PG 的公共子集。

### 域类型（`internal/domain/account.go`）

```go
type Role string // RoleViewer < RoleEditor < RoleOwner；Role.AtLeast(min Role) bool；非法值 Valid()==false

type Account struct {
    ID, Username, DisplayName, Email, PasswordHash string
    PlatformAdmin, Disabled bool
    CurrentTenantID string // 账号级当前项目；会话客户端存在 Session 上
    CreatedAt time.Time
    LastLoginAt *time.Time
}
type Membership struct { AccountID, TenantID string; Role Role; CreatedAt time.Time }
type Session struct {
    ID string; Hash [32]byte; AccountID, CurrentTenantID string
    CreatedAt, ExpiresAt time.Time; LastSeenAt, RevokedAt *time.Time
}
type Credentials struct{ Bearer, Session string } // 来自 Authorization 头与会话 cookie

type Principal struct {
    Kind          PrincipalKind // PrincipalAccount | PrincipalAdminToken
    AccountID     string
    Username      string        // 管理 Token 为 "admin-token"
    DisplayName   string
    SessionID     string
    TokenID       string
    PlatformAdmin bool          // 管理 Token 恒为 true
    CurrentTenant string
    Roles         map[string]Role // 项目 ID → 角色，不含平台管理员的隐含权限
}
func (p Principal) RoleIn(tenantID string) Role                // 平台管理员对任意项目返回 RoleOwner；非成员返回 ""
func (p Principal) Require(tenantID string, min Role) error    // 不足返回 ErrForbidden（消息不泄露项目是否存在）
func (p Principal) RequirePlatformAdmin() error
```

`Token` 增加 `CreatedByAccountID string`，仅用于审计；`ErrRateLimited` 映射 HTTP 429。

### 服务（`internal/service/account`）

```go
type Options struct {
    SessionTTL         time.Duration // 默认 12h，空闲超时
    SessionMaxLifetime time.Duration // 默认 720h（30 天），绝对上限，必须 >= SessionTTL
    SeedUser           string        // 默认 "admin"
    SeedPassword    string        // 空 = 不种子
    Argon2          *argon2id.Params // nil 用 OWASP 推荐值（m=19MiB,t=2,p=1）；测试可调小
    MaxFailures     int           // 默认 5
    FailWindow      time.Duration // 默认 10m
    InviteDefaultTTL time.Duration // 默认 168h（7 天）
    InviteMaxTTL     time.Duration // 默认 720h（30 天），且不得大于 720h
    External        ExternalOptions // 零信任外部身份；Enabled=false 时完全忽略
}

// ExternalOptions 决定网关身份如何映射到账号。
type ExternalOptions struct {
    Enabled      bool
    Required     bool     // true：只认外部身份或管理 Token，本地会话不再单独通过
    AutoCreate   bool     // true：未知 username 自动建无口令账号
    AllowedUsers []string // 空表示不限制
}

// Option 在默认构造之后调整服务：WithLogger 注入日志器，WithThrottler 替换登录节流后端。
type Option func(*Service)

func WithLogger(log *slog.Logger) Option
func WithThrottler(t Throttler) Option
func New(store Store, admin AdminAuthenticator, opts Options, options ...Option) (*Service, error)

func (s *Service) Bootstrap(ctx context.Context) error // 种子管理员 + 个人项目回填 + 清理过期会话/邀请
func (s *Service) Login(ctx context.Context, username, password, remoteIP string) (raw string, sess domain.Session, err error)
func (s *Service) Authenticate(ctx context.Context, c domain.Credentials) (domain.Principal, error)
func (s *Service) Logout(ctx context.Context, rawSession string) error
func (s *Service) SwitchProject(ctx context.Context, p domain.Principal, tenantID string) error

func (s *Service) CreateAccount(ctx context.Context, in CreateAccountInput) (domain.Account, error)
func (s *Service) UpdateAccount(ctx context.Context, actorAccountID, id string, in UpdateAccountInput) (domain.Account, error)
func (s *Service) ResetPassword(ctx context.Context, id, password string) error
func (s *Service) ListAccounts(ctx context.Context) ([]domain.Account, error)
func (s *Service) ListAccountsPage(ctx context.Context, query string, page, size int) ([]domain.Account, int, error)
// MemberInput 是成员变更入参；ActorAccountID 为空表示这次变更不受“不能降级自己”约束。
type MemberInput struct {
    ActorAccountID string
    TenantID       string
    AccountID      string
    Role           domain.Role
}

// PasswordChangeInput 是改密入参；RemoteIP 只用于登录失败节流。
type PasswordChangeInput struct {
    AccountID   string
    SessionID   string
    OldPassword string
    NewPassword string
    RemoteIP    string
}

func (s *Service) ChangePassword(ctx context.Context, in PasswordChangeInput) error
func (s *Service) SetMember(ctx context.Context, in MemberInput) error
func (s *Service) RemoveMember(ctx context.Context, actorAccountID, tenantID, accountID string) error
func (s *Service) ListMembers(ctx context.Context, tenantID string) ([]domain.Membership, error)
func (s *Service) MemberCandidates(ctx context.Context, actor domain.Principal, tenantID, query string) ([]domain.MemberCandidate, error)
func (s *Service) RevokeSessions(ctx context.Context, accountID string) error
```

个人项目的创建与回填也在本服务内：`ensurePersonalTenant(ctx, acct)` 幂等创建 `u-<accountID>` 项目并写入 owner 关系，不主动改账号的 `current_tenant_id`；账号创建路径尽力调用，`Bootstrap` 对所有启用账号回填，登录时 `currentTenant` 在保存值为空或不可访问时优先回退到自己的个人项目。邀请方法见 [invites](invites.md)。

不变量：

- 口令：长度 8..128 字节；用 `github.com/alexedwards/argon2id`（PHC 字符串、盐、常量时间比较都由库处理），不手写密码学。用户名先 `TrimSpace` 再转小写，只允许 `[a-z0-9._@-]{1,64}`。
- 登录对“账号不存在 / 口令错 / 账号已禁用”返回同一条 `ErrUnauthorized` 消息；账号不存在时仍对固定哑哈希做一次校验，抹平耗时差。口令与用户名之外的输入不进日志，口令永不进日志、审计、错误消息。
- 节流：同一用户名或同一来源 IP 在 `FailWindow` 内失败 `MaxFailures` 次后返回 `ErrRateLimited`，窗口过后恢复，成功登录清零该用户名的计数；计数默认在进程内存（多副本各自计数），也可以通过 `MEMOSD_REDIS_URL` 放到 Redis 共享；Redis 出错时按单次调用回退到内存，登录不会被拒。内存实现条目有上限，超限时丢弃最旧的；Redis 计数按窗口 TTL 过期。
- 会话 Token 是 32 字节随机数（`crypto/rand`，base64url），库里只存 SHA-256。语义是**空闲超时 + 绝对上限**：登录时 `expires_at = now + SessionTTL`；每次请求（节流间隔为 `min(1m, SessionTTL/2)`，最少 1s）把 `expires_at` 延到 `min(now+SessionTTL, created_at+SessionMaxLifetime)`，因此活跃用户不会在登录 12h 后被踢，而空闲超过 12h 或到达 30 天绝对上限即失效。节流间隔必须小于 TTL 的一半：若固定 1m，把 `SessionTTL` 配成 1m 时下一次续期机会正好落在过期点上，活跃用户也会被登出。`last_seen_at` 与 `expires_at` 在同一个 `ExtendSession` UPDATE 里写；已吊销（`NULL` 或空串）或已过期的会话不会被续期。登录、登出、改密、重置口令、成员变更、会话吊销后清空 10s 主体缓存，禁用账号走同一事务吊销全部会话，过期/吊销/禁用都不会被滑动续期“复活”。缓存条目同时记住建立时的会话过期时间，命中时也要校验：会话自然过期后不会因为还在 10s 缓存窗口内而继续通过鉴权。启动时与登录时（最多每小时一次）清理过期超过 24h 的会话。
- 账号状态变更的两次写入有明确边界：**由启用转禁用**（`UpdateAccount` 里 `disabled` 从 false 变 true）与**管理员重置口令**（`ResetPassword`）都把“更新账号行”和“吊销该账号全部未吊销会话”放进同一个事务（`UpdateAccountAndRevokeSessions`），任一步失败整体回滚。因此不会出现“账号已禁用但旧会话仍可登录”或“口令已更换但旧会话仍有效”的中间态；其它字段的更新仍走单条 `UpdateAccount`。事务提交成功后才清登录节流（禁用清该用户名计数、重置清全部计数）与主体缓存。
- `Authenticate`：Bearer 存在时只按管理 Token 解析（成功得到 `PrincipalAdminToken`，`PlatformAdmin=true`，没有会话）；否则按会话 cookie 解析，校验未吊销、未过期、账号存在且未禁用，并加载成员关系（只含未停用的项目空间）。两者都没有或无效返回 `ErrUnauthorized`。当前项目取会话保存的项目空间（外部身份没有会话，取账号级的 `current_tenant_id`）；它不存在、已停用或已无权限时回退为可访问项目里 ID 最小者；没有任何可访问项目则为空串。
- 外部身份分支（`External.Enabled`）：`Credentials.External` 非空时优先于会话解析，得到不带 `SessionID` 的主体，当前项目取账号级偏好（没有则按上面的回退规则）。username 必须是合法账号名（大小写归一），否则 `ErrUnauthorized`；命中 `AllowedUsers` 白名单之外、账号已禁用、账号不存在且 `AutoCreate=false` 都返回 `ErrForbidden`，不泄露账号是否存在。`AutoCreate=true` 时以空 `PasswordHash` 建号（口令登录永远失败），显示名取网关的 `name`、邮箱取 `email`（`name` 为空回退 username，`email` 为空留空）；已有账号在网关资料变化时补齐这两列，只在值不同时写一次；并发建号撞唯一键时回读既有账号。
- 外部身份不写会话、不发 cookie、不更新 `last_login_at`（每次请求都由网关重新认证）。账号行仍每次查库，禁用立即生效；成员关系与会话共用 10s 主体缓存，成员增删、账号启停、切换项目都会清空缓存。`SwitchProject` 对它们写账号级 `current_tenant_id`，因此多项目用户仍可切换。`External.Required=true` 且请求没有外部身份时，只有管理 Token 能通过，会话 cookie 返回 `ErrUnauthorized`。
- `SwitchProject` 要求会话主体（管理 Token 没有会话，返回 `ErrInvalidInput`）、目标项目空间存在且未停用、未归档、主体对它至少是 viewer（平台管理员对任意启用的项目空间）。只改本会话，不改项目空间本身。
- 种子管理员：`SeedPassword` 非空且库里没有同名账号时创建为 `platform_admin`；已存在且已有口令则什么都不做，不覆盖口令、不恢复被禁用的状态。已存在但**没有口令**（零信任自动建号留下的空口令账号）时补上种子口令并提升为平台管理员，启动日志打 WARN。这是为了不让种子配置因同名自动建号而静默失效；前置条件是该用户名与网关身份同名，部署时应避免用普通用户可占用的名字。新账号（含种子、管理员建号、零信任自动建号）都会尽力创建个人项目，失败只记 WARN，由下次认证或启动回填修复。
- 至少保留一个启用的平台管理员账号：把最后一个启用的平台管理员禁用或降级返回 `ErrConflict`。平台管理员**不能把自己**的 `platform_admin` 置 false，也不能把自己 `disabled` 置 true；其他平台管理员仍可在非最后一位时降级/禁用他。
- `SetMember` 要求账号与项目空间都存在、角色合法；重复调用是改角色。`RemoveMember` 不存在返回 `ErrNotFound`。
- 直接按用户名把账号设为 owner（`POST /admin/tenants/{id}/members {role:"owner"}`）允许项目 owner 自己操作；邀请链接的 `role=owner` 仍只允许平台管理员创建。两者规则有意不同：前者是显式指名、可审计的操作，后者是无主 bearer 链接，泄露后任何人都能兑换。
- 每个项目至少保留一个 owner：把最后一个 owner 降级或移出返回 `ErrConflict`；平台管理员也不能绕过这条规则，需要先加一个 owner。创建项目时，若调用主体是账号（控制台登录），服务端自动把创建者落成 owner；管理 Token 没有账号身份，必须在 body 里给 `ownerUsername`，否则 400，避免产生无主项目。
- **任何人不能撤销自己的项目管理员权限**：`SetMember` 把 actor 自己降为非 owner，或 `RemoveMember` 移出 actor 自己，返回 `ErrConflict`。多个 owner 存在时也一样；要退出项目，必须先由另一位 owner 或平台管理员操作。最后一位 owner 保护继续生效。
- 个人项目：`tenants.kind='personal'`，`id` 与 `user_id` 都是 `u-<accountID>`，名称取 `<displayName> 的空间`（displayName 为空时用 username），创建者自动 owner；账号没有可用的 `current_tenant_id` 时，当前项目回退到它。默认私有、不自动共享，但 owner 可以改名、改描述、邀请或加成员；**被加入的成员能在项目列表里看到它**，非成员（含平台管理员）看不到。**owner 不能停用个人项目**（`project/revoke` 或 `project/update status=DISABLED` 返回 409），平台管理员仍可通过 `/admin/tenants` 停用。普通项目 ID 保留 `u-` 前缀。
- 任意已登录账号都可以创建团队项目，创建者自动成为 owner；创建入口不再要求平台管理员。个人项目不占这条限制，也允许 owner 建更多团队项目。
- 成员搜索：`MemberCandidates` 只允许项目 owner（平台管理员等价），去掉查询串开头的 `@`，按 username 前缀 + display_name 子串匹配，最多 10 条，返回 `accountId`、username、displayName、disabled、member；查询去掉 `@` 后为空返回空列表；只有 1 个字符时只做用户名**精确**匹配（兼容 1 字符用户名），2 个字符起才做前缀/子串搜索，避免逐字枚举。产品上接受“登录用户可以按 2 字符前缀搜索到账号”这一取舍，接口不提供全量目录，且每次搜索都会写审计。找不到账号时前端引导发邀请链接。
- 自服务只接受账号会话（管理 Token 没有会话，返回 400）：改口令要校验原口令，成功后吊销其他会话；原口令错误也计入登录节流（用户名 + IP），超过阈值返回 429；查看/吊销会话只作用于自己。
- 账号列表分页（`GET /admin/accounts`）：`query` 同时匹配用户名与显示名的子串，ASCII 大小写不敏感（SQLite `LIKE` 默认行为），中文不受影响，但非 ASCII 拉丁字母（如 `Ä`/`ä`）不会被折叠——搜索这类名字要按原大小写输入；`page` 从 1 起，`pageSize` 默认 20、上限 200。过滤与分页在 store 的 SQL 里完成，`COUNT` 与当页查询在同一个只读事务里，保证 `total` 与当页条数一致；排序固定 `created_at ASC, id ASC`；`%`、`_` 按字面匹配。旧实现先全量 `ListAccounts` 再在 Go 里过滤切片，账号多时每次翻页都读整表，已下沉。
- 项目 `user_id` 唯一：`tenants.user_id` 直接映射上游 `user_name`，非空值由 `tenants(user_id) WHERE user_id <> ''` 部分唯一索引保证不重复（迁移 `0016`）。管理端 `POST /admin/tenants` 把某项目的 `user_id` 改成另一个项目已占用的值时返回 409“该上游 user_id 已被其他项目占用”，不会静默串记忆数据；`CreateTenant`/`EnsureTenant` 的同名 ID 冲突与 `user_id` 冲突有不同消息。空 `user_id` 仍允许多行（store 的写入口会拒绝空值，索引只兜底历史行）。

### 登录入口（`internal/transport/auth`，挂在 console 角色，不需要凭据）

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/__login` | 返回自带样式的登录页（只有账号 + 口令一个表单，不引外部资源；错误文案是固定字符串，不回显输入）；可选 `next` 只接受站内路径，写进隐藏字段 |
| POST | `/__login` | 表单 `username`、`password`、可选 `next`（`application/x-www-form-urlencoded`）：成功 303 到安全的 `next`（默认 `/`）并写会话 cookie，失败 401 + 登录页且保留 `next`，限流 429；`application/json` 同字段时返回信封 `{code:0,data:{accountId,username,displayName,platformAdmin,currentProjectId}}`（给脚本与 e2e 用），JSON 登录不跟随 `next` 跳转 |
| GET / POST | `/__logout` | 吊销当前会话，清 cookie；配置了 `MEMOSD_ZEROTRUST_LOGOUT_URL` 时同时清网关 cookie 并 303 到该地址，否则 303 到 `/`；无会话也成功 |
| GET | `/api/auth/me` | 需要登录；`{accountId,username,displayName,platformAdmin,currentProjectId,projects:[{id,name,role,member,kind}]}`；管理 Token 的 `accountId` 为空串；清单外扩展，前端使用 |
| GET / POST | `/__invite/{token}` | 邀请落地页与兑换，见 [invites](invites.md) |
| POST | `/api/auth/password` | 账号会话；`{oldPassword,newPassword}`；原口令错 401，新口令不合法 400；成功后保留当前会话、吊销其他会话 |
| GET | `/api/auth/sessions` | 账号会话；`{sessions:[{id,current,createdAt,expiresAt,lastSeenAt,projectId}],total}`，不含 Token |
| POST | `/api/auth/sessions/revoke` | 账号会话；吊销除当前会话外的全部会话，返回 `{revoked:n}` |

- 登录页与登录响应带 `Cache-Control: no-store`、`X-Frame-Options: DENY`、`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`。
- 未登录访问 `/api/*` 与 `/admin/*`：401 信封 `{code:40100,data:null,message:"登录已过期，请重新登录"}`，不回登录页（前端只认信封）。登录入口是 `/__login`；前端目前未登录时只显示接口 401 错误态，不自动跳转，这一项记为“待前端处理”。
- 审计：`auth.login` 每次尝试写一条，`auth.logout` 只在确实吊销了一个未过期、未吊销的会话时写一条；`auth.password`、`auth.sessions.revoke` 记录自服务改口令与会话吊销（用户名、来源 IP、是否成功），不含口令；失败的用户名截断到 64 字符。伪造或已失效的 cookie 访问 `/__logout` 不写审计。Dashboard 与 admin 的 `wrap` 只为已认证主体写审计，未登录请求（含 401 与匿名的 `/api/meta`）不写，避免匿名流量灌满审计表。

### 角色矩阵（Dashboard 与 admin 共用，平台管理员在任何项目都等价于 owner）

| 能力（Capability） | viewer | editor | owner | 平台管理员 |
|------|:---:|:---:|:---:|:---:|
| `CapProjectView`：记忆列表/详情、图谱、日志、用量、画像模板、抽取 prompt、Key 列表（仅前缀掩码） | 是 | 是 | 是 | 是 |
| `CapProjectWrite`：删除记忆、画像模板增改删、创建/吊销/改名 Key、按需复制 Key 明文、保存/重置/生成/调试抽取 prompt | 否 | 是 | 是 | 是 |
| `CapProjectManage`：改名/停用项目、成员增删与改角色、邀请创建/撤销、成员搜索、项目描述/多视图 | 否 | 否 | 是 | 是 |
| `CapProjectCreate`：创建项目 | 是 | 是 | 是 | 是 |
| `CapPlatformManage`：账号管理、查看全部项目 | 否 | 否 | 否 | 是 |
| 切换当前项目 | 是（限已加入的，写会话） | 是 | 是 | 是（写账号级偏好） |

`Role.Can(c)` 是唯一的能力真值表，`Principal.Can(tenantID, c)` 在其上叠加平台管理员与登录判断；Dashboard/admin 路由、handler 例外和 service 都用它，不再各自比较角色字符串。viewer/editor/owner 的 rank 仍是 `viewer < editor < owner`，非法角色永远不满足任何能力。

“项目”按请求里的 `projectId`（或 `tenant`、路径参数）确定；Key 的写入按 Key 所属项目判定，不信任请求里自报的项目。非成员访问一律 403，不区分“不存在”和“无权限”。抽取 prompt 已按项目保存在上游，读取要求 viewer，保存/重置/生成/调试要求 editor（后两者会消耗上游模型调用）。

Dashboard 逐路由的最低要求写在 `dashboard-routes.md`；admin 路由：

| 方法 | 路径 | 最低要求 |
|------|------|----------|
| GET / POST | `/admin/tenants` | 平台管理员；POST 为整体覆盖，`description`/`isMultiView` 可写，未提供时保留原值 |
| GET | `/admin/tenants/{id}/tokens` | 该项目 viewer |
| POST | `/admin/tenants/{id}/tokens` | 该项目 editor |
| POST | `/admin/tokens/{id}/revoke` | Key 所属项目 editor |
| GET | `/admin/accounts` | 平台管理员；`query` 过滤用户名/显示名，`page`、`pageSize`（默认 1、20，上限 200） |
| GET | `/admin/accounts/{id}` | 平台管理员；单个账号，响应不含口令哈希 |
| POST | `/admin/accounts` | 平台管理员；`{username,password,display_name,platform_admin}` |
| POST | `/admin/accounts/{id}` | 平台管理员；`{display_name,disabled,platform_admin}` 任选；显示名不能为空；不能自降平台管理员或自我禁用 |
| POST | `/admin/accounts/{id}/password` | 平台管理员；`{password}`，同时吊销该账号全部会话并清掉登录失败计数 |
| POST | `/admin/accounts/{id}/sessions/revoke` | 平台管理员 |
| DELETE | `/admin/accounts/{id}` | 平台管理员；删除账号。不能删自己，不能删最后一个启用的平台管理员。个人项目连同其成员/邀请/API Key 一起删除；团队项目只移除成员关系，该账号创建过的 Key 与邀请保留并把创建者引用清空；审计记录保留 |
| GET | `/admin/tenants/{id}/members` | 该项目 owner |
| POST | `/admin/tenants/{id}/members` | 该项目 owner；`{account_id 或 username, role}`；不能自降/自移 |
| POST | `/admin/tenants/{id}/members/{accountId}/remove` | 该项目 owner；不能移出自己 |
| GET | `/admin/tenants/{id}/member-candidates` | 该项目 owner；`query` 支持 `@username`，最多 10 条 |
| GET / POST | `/admin/tenants/{id}/invites` | 该项目 owner；见 [invites](invites.md) |
| POST | `/admin/tenants/{id}/invites/{inviteId}/revoke` | 该项目 owner；见 [invites](invites.md) |

### 当前项目语义（修正与前端的对接）

前端把 `project/dict` 里第一个 `status==="ENABLED"` 当作当前项目。因此：`project/dict|list` 只让当前项目返回 `ENABLED`，其余可访问项目返回 `DISABLED`（不代表被停用）；切换走 `project/update current=true`，有会话写会话，零信任等外部身份写账号级 `current_tenant_id`（它们没有会话）。`project/update status=ENABLED` 是**恢复停用项目**，不再顺带切换。项目空间本身的停用走 `project/revoke/{id}`（owner）与 `/admin/tenants`：停用项目不出现在侧栏 `dict`（平台管理员也一样）；`project/list` 仍返回给真实成员与平台管理员，带 `disabled:true` 与真实 `role`，owner 可经 `status=ENABLED`（完整成员关系判权）重新启用。

归档（软删除）是另一个状态：`tenants.archived_at` 非空，同时 `disabled=1`；store 的 `normalizeArchived` 在所有写入路径强制这个不变量，运维 upsert 不传 `disabled` 也不会把归档项目改回可用。`project/dict` 与 `/api/auth/me` 不返回归档项目；`project/list` 仍返回并带 `archived:true`/`archivedAt`，owner 与平台管理员可以恢复到启用状态。归档不删成员、邀请与 API Key，恢复后原样可用；判权用完整成员关系而不是 `Principal.Roles`（后者会过滤停用项目）。`SwitchProject`、`CreateInvite`、`RedeemInvite` 在检查 `disabled` 之前先拒绝 `Archived()`，避免历史脏行或未来新增写入路径把归档项目当成普通停用项目放进来。

新账号的个人项目在首次认证前/时创建，`current_tenant_id` 为空时指向它，因此新用户第一次打开控制台就有一个可写空间，而不是空项目列表。`project/dict|list` 每行带 `kind`（`team`/`personal`），前端把个人项目标为“我的空间”并排在前面。个人项目不能被 owner 停用；平台管理员仍可通过 `/admin/tenants` 停用。邀请兑换成功后，当前项目切到邀请所属项目（账号级与会话级都写），下一次 `/api/auth/me` 立即反映。

### Key 归属

- 一个 Key 属于且仅属于一个项目空间（`tokens.tenant_id`），同一空间可建多把；Key 不属于任何账号，`created_by_account_id` 只记创建者（管理 Token 创建则为空串）。
- `keys/list` 只返回 `projectId` 这个项目空间的 Key，且要求该项目 viewer；账号看不到自己没加入的项目空间的 Key。列表始终给前缀掩码，并按写权限给 `canCopy`（库里留有明文且调用者能写）与 `lastUsedAt`。`keys/copy` 要求项目写权限，按 id 单把返回明文——viewer 拿到 API Key 等于绕过角色直接写记忆；历史 Key 没有明文时返回 409，提示重新生成。`keys/rename/{id}` 同样要求写权限，只改展示名，不影响哈希、前缀与过期时间。
- `keys/revoke/{id}`、`/admin/tokens/{id}/revoke` 先按 Key ID 取其所属项目，再判 editor，所以 Key ID 猜中也不能跨项目吊销。
- `identity.Service.IssueToken(ctx, tenantID, label, createdBy string)` 增加创建者参数；`GetToken(ctx, id)` 新增，供 transport 解析 Key 所属项目；`RenameToken(ctx, id, label)` 改展示名。
- 控制台不能用 API Key 登录。

### 配置（新增，写入 `config.md`）

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `MEMOSD_ADMIN_USER` | `admin` | 种子管理员用户名，仅当设置了口令时生效 |
| `MEMOSD_ADMIN_PASSWORD` | 空 | 非空且 ≥ 8 字节时，库里没有同名账号则创建为平台管理员；摘要里脱敏 |
| `MEMOSD_SESSION_TTL` | `12h` | 会话空闲超时，≥ 1m；续期节流为 `min(1m, TTL/2)`，最少 1s |
| `MEMOSD_SESSION_MAX_LIFETIME` | `720h` | 会话绝对上限（30 天），必须 ≥ `MEMOSD_SESSION_TTL`，否则启动失败；也是登录 cookie 的 `Max-Age` |
| `MEMOSD_COOKIE_SECURE` | `true` | 会话 cookie 是否带 `Secure` |

### 会话与账号设计

- 会话 cookie 名 `memos_console_sid`，`HttpOnly`、`SameSite=Strict`，默认 12h 空闲超时（绝对上限 30 天）；`/__login` `/__logout` 路径与 303 跳转保持不变；登录页自带样式，不引外部资源。
- 账号入库、可增删、可属多个项目空间；会话在服务端，可吊销。
- `/api/meta` 未登录只给 `mode/service/keyPrefix/tools/authEnabled/authMode`，登录后才多给 `sources`。
- 控制台不支持 API Key 登录：Key 只属于一个项目，允许登录会绕过项目边界；确有需要时，可作为单项目、editor 角色的 Key 会话另立决策。

## 实现与设计的差异

落地时相对上文设计做了以下调整，均已被 e2e 覆盖：

- **共享授权引擎**：`Rule`/`Scope` 放在 `internal/transport/auth`，dashboard 与 admin 共用，避免两处各写一套角色判断。范围有 `Authed`、`Public`、`Platform`、`InBody`、`InQuery`、`InPath`、`OnKey`；Key 范围先经 `GetToken` 解析到所属项目。找不到的 Key 对非平台管理员返回 403（不泄露存在性），对平台管理员返回 404。
- **项目解析顺序**：body.projectId → query.projectId → 会话当前项目。原设计要求显式 `projectId`，但前端的 `memory/detail`、`memory/delete`、`log/detail`、`keys/copy` 都不带它，所以回落到当前项目；授权通过后把项目写回请求体，handler 不再自己取项目。
- **`Accounts` 端口**：dashboard 与 admin 各声明自己需要的 `Accounts` 接口（`Authenticate`、`SwitchProject` 等），由 `service/account` 满足，替代了原来的 `Identity.AuthenticateAdmin`。
- **`domain.SafeMessage(err)`**：错误信封只取 `Error.Message`，不带 `Op:` 前缀，避免把内部操作名返回给浏览器。`ErrRateLimited` 映射 HTTP 429 / 信封码 40000。
- **`Principal.Actor()`**：审计里的主体名，账号取用户名，管理 Token 取 `admin-token`，未认证为空串（据此跳过审计）。
- **`ResolveAccount(idOrUsername)`**：`POST /admin/tenants/{id}/members` 的 body 同时接受 `account_id` 与 `username`，由 service 解析，handler 不碰存储。
- **`/api/meta`**：未登录返回公开子集，登录后才带 `sources`；`authEnabled` 恒为 `true`，`authMode` 默认 `"page"`，零信任必选（`MEMOSD_ZEROTRUST_REQUIRED=true`）时为 `"external"`。外部模式下前端仍显示退出入口：`/__logout` 会清 `SECURE_ZT_GW_TOKEN` 并跳到 `MEMOSD_ZEROTRUST_LOGOUT_URL`（dev 配成 UUAP 退出页）。灰度态（`Enabled=true`、`Required=false`）仍返回 `page`。
- **会话清理**：启动时与登录路径上（最多每小时一次）清理过期 24 小时以上的会话，不另起后台 goroutine。
- **种子管理员**：只在库里没有同名账号时创建，永不覆盖已有口令（`TestE2ESeedAdminIsNotOverwritten` 钉住）；唯一的例外是同名账号存在但口令哈希为空，此时补种子口令并提权（见上文）。

## 错误与边界情况

- 存储不可用返回 5xx，不伪装成 401。会话已过期、被吊销、账号被禁用都是 401。
- 项目空间不存在或已停用：`SwitchProject` 返回 404/403；成员关系指向已停用项目空间时，该项目不进入 `Roles`。`SetMember`/`RemoveMember` 允许操作已停用的项目空间，成员关系在重新启用后生效。
- `project/update` 先授权后产生副作用：只带 `id` 是读取，要求 viewer；改名或 `status=DISABLED` 要求 owner；`status=ENABLED` 是恢复停用项目（owner），`current=true` 是切换当前项目（viewer）；`status` 缺省或 null 视为未给，其余只接受字符串 `ENABLED`/`DISABLED`，其他值（含非字符串）400。授权失败的请求不改动会话。停用项目的 owner 的 `Roles` 为空，恢复判权改用完整成员关系；非 owner 成员仍得到 403。
- 种子口令过短或用户名非法：启动失败（配置校验在 `internal/config` 做，service 再校验一次）。
- 限流返回 429，信封码 `40000`，消息“尝试次数过多，请稍后再试”；限流状态不落库，重启清零。来源 IP 只取 `RemoteAddr`，不信任 `X-Forwarded-For`（不可伪造）；若控制台经反向代理或 ingress 暴露，所有人会共享代理的 IP，5 次失败即让全站登录 429 十分钟。直连或集群内访问不受影响，接入反代前需要先加“受信代理”配置，记为部署决策点。429 在配置了 `Auth.LoginFailWindow`（或 account 服务默认窗口）时带 `Retry-After`：值是窗口长度的上限（秒，向上取整，至少 1），不是精确剩余时间，客户端按它退避即可；未注入窗口时（如少量测试替身）不设该头。
- 平台管理员自降/自禁用：**不允许**。`UpdateAccount` 把 actor 自己的 `platform_admin` 置 false 或 `disabled` 置 true 返回 409；其他平台管理员在非最后一位时仍可操作。管理 Token 始终可用，是应急通道。
- 项目 owner 自降/自移：**不允许**，无论是否还有其他 owner；返回 409。要退出项目，先让另一位 owner 或平台管理员操作。最后一位 owner 保护继续生效。若最后一个 owner 账号被禁用，owner 行仍在（因此仍受“至少一个 owner”保护），但该账号无法登录；平台管理员需要先补一个 owner，再处理这个账号。删除账号走 `DELETE /admin/accounts/{id}`，成员关系在同一事务里清除；删除后不会留下指向已删账号的成员行。
- 重复 `user_id`：管理端改绑到已被占用的上游 `user_id` 返回 409，消息“该上游 user_id 已被其他项目占用”。若**历史库**在迁移 `0016` 之前已存在重复非空 `user_id`，迁移会失败、进程拒绝启动；需要先人工合并重复项目或改绑其中一个 `user_id`，再启动。这是有意的“失败要响”，因为共享 `user_name` 会让两个项目互相看到/写入对方的记忆。
- 个人项目：回填/创建是幂等的；已有同 ID 的非个人项目时记 WARN 并不覆盖。个人项目被平台管理员停用后不会自动重建（尊重管理员操作）；owner 自己不能停用。个人项目的 `user_id` 是 `u-<accountID>`，与团队项目 ID 空间隔离，避免上游 `user_name` 混数据。
- 项目创建在同一事务里写租户行与创建者的 owner 成员行（`CreateTenantWithOwner`）：同名冲突或成员写入失败都整体回滚，不会留下无 owner 的项目。管理 Token 必须给 `ownerUsername`，否则 400，从入口上排除无主项目。本次改造前遗留的历史无 owner 项目仍可由平台管理员恢复：`POST /admin/tenants/<项目>/members`，body `{"username":"<账号>","role":"owner"}`。
- 控制台项目页提供停用/启用与归档/恢复入口；停用后的恢复走 `project/update status=ENABLED`（owner，完整成员关系判权），归档后的恢复走 `project/restore`（owner 或平台管理员）。两者都不自动切换当前项目。

## 测试与验证

- 会话：`sliceSessionExpiry` 的边界（`now+TTL` 与 `created_at+MaxLifetime` 取小）；touch 后 `expires_at` 前移、到达绝对上限后不再延长；过期/吊销/禁用账号仍然 401；`SessionMaxLifetime < SessionTTL` 时 `account.New` 返回错误。
- 黑盒 e2e（`internal/app` 包内，真实 SQLite + 真实 HTTP，不 mock 存储与身份；审计断言用 `audit.Stop` 同步排空后读 `ListAudit`）：未登录 401；错误口令 401 且不区分原因；5 次失败后 429；登录成功得 cookie；非成员 403；viewer 创建 Key 403；editor 管成员 403、owner 可以；禁用账号后旧会话 401 且不能再登录；登出后旧 cookie 401；切换项目只改本会话（两个会话互不影响），零信任主体切换写账号级偏好并在下次请求生效；`keys/list` 只含当前项目的 Key；用另一项目的 Key ID 吊销被拒；管理 Token 仍可管理；最后一个平台管理员不能被禁用；创建者自动成为 owner；owner 自降/自移 409（多 owner 时也一样）；平台管理员自降/自禁用 409；任意账号可建项目；新账号自动拥有个人项目且 `currentProjectId` 指向它；个人项目 owner 不能停用、平台管理员可以；管理 Token 建项目缺 `ownerUsername` 400；成员候选仅 owner 可用、`@` 搜索不返回全量；停用项目对 owner/成员可见且 owner 可自行启用、viewer 不能启用；个人空间对被加入的成员可见、管理员非成员不可见；归档项目经 upsert 仍保持停用，创建/兑换邀请与切换项目都被拒；管理 Token 建项目响应不写成 owner；`/api/meta` 的全局计数仅平台管理员可见。
- 少量单元测试只覆盖不方便经 HTTP 钉住的纯逻辑：`Principal.RoleIn/Require/Can`、`Role.Can` 真值表、口令/用户名校验、节流的用户名与来源 IP 两个维度及窗口过期（e2e 里所有请求来源 IP 相同，无法分开验证）。
- 邀请链路的 e2e 与 store 单测见 [invites](invites.md)。
- 账号列表与项目 `user_id`：store 单测覆盖账号多于一页时的过滤、过滤后总数、分页与排序，以及 `%`/`_` 的字面匹配；`tenants.user_id` 非空重复返回可辨认的冲突、空值允许多行；admin 单测覆盖重复 `user_id` 的 409 与消息。
- 会话只存哈希由类型保证：`domain.Session` 只有 `Hash [32]byte`，没有明文字段，`SessionByHash` 是唯一的查找入口。
- golden：`test/golden/dashboard/routes.json` 钉住 Dashboard 路由的状态码与信封键；账号相关响应形状由 e2e 断言钉住，不另建 golden。

## 参考来源

- `docs/module/identity.md`、`docs/module/admin.md`、`docs/module/dashboard-routes.md`（本模块落地后同步）。

## 实现记录

See the public version-control history.
