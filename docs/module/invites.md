---
module: invites
packages:
  - internal/service/account
  - internal/transport/auth
  - internal/transport/admin
  - internal/adapter/store
  - internal/domain
status: implemented
---

# invites

项目邀请链接：项目 owner（或平台管理员）生成一条带角色的通用链接，被邀请人登录后兑换为 `project_members` 一行；链接单次使用、可过期、可撤销。它解决“新用户还不知道自己该加入哪个项目、owner 也只能精确输入用户名”的问题，与 [accounts](accounts.md) 的账号、成员和 ACL 共用同一套身份与角色。

status 含义：`draft` 还在写，不得写代码；`accepted` 契约已定，可以开始实现；`implemented` 代码与测试已落地并与本文一致。

## 职责与边界

- 负责：邀请的表结构、token 生成与哈希、创建/列表/撤销/预览/兑换、单次消费的原子性、过期与角色规则、邀请落地页、登录后的安全回跳、邀请相关审计。
- 不负责：账号创建与禁用（accounts / zerotrust）、项目成员角色矩阵本身（accounts）、项目启停（dashboard/admin）、邮件或 IM 发送（本模块只生成链接，由人复制转发）。
- 邀请链接是 bearer 凭证：拿到链接的人就能以指定角色加入项目。本模块不绑定具体人，这是有意的产品选择；用短有效期、单次使用、角色上限和审计降低泄露影响。

## 依赖

- `internal/domain`：`ProjectInvite`、`InviteStatus`、`NewInviteToken`、`Role`、`Capability`、typed errors。
- `internal/service/account`：邀请方法放在账号服务里，因为它直接写 `project_members` 并复用 `Principal`、租户与账号校验；存储端口由该包声明。
- `internal/adapter/store`：`project_invites` 的 CRUD 与事务内“标记已用 + 写成员 + 切当前项目”。
- `internal/transport/admin`：owner 创建/列表/撤销邀请的管理路由。
- `internal/transport/auth`：`/__invite/{token}` 落地页、`/__login` 的 `next` 回跳、邀请兑换审计。
- `internal/config`：邀请默认有效期与上限。

## 契约

### 数据模型（迁移 `0013_project_invites.sql`）

```sql
CREATE TABLE IF NOT EXISTS project_invites (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    role TEXT NOT NULL,                    -- viewer | editor | owner
    token_hash BLOB NOT NULL UNIQUE,       -- SHA-256(raw token)；明文只在创建响应出现一次
    created_by_account_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,                          -- 非空即已使用
    used_by_account_id TEXT NOT NULL DEFAULT '',
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invites_tenant ON project_invites(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invites_expires ON project_invites(expires_at);
```

不变量：

- token 是 32 字节 `crypto/rand`，格式 `inv_<64 hex>`；库里只有 `SHA-256(raw)`，结构体没有明文字段。
- `role` 必须是 `viewer`、`editor`、`owner` 之一；`owner` 角色的邀请只有平台管理员能创建。
- `used_at` 非空、`revoked_at` 非空、`expires_at <= now` 任一成立即不可兑换；状态按 revoked → used → expired → active 的顺序判定。
- 过期时间默认 7 天（`MEMOSD_INVITE_DEFAULT_TTL=168h`），上限 30 天（`MEMOSD_INVITE_MAX_TTL=720h`）；创建时超过上限返回 400，不静默截断。
- 邀请只属于一个项目；一个项目可有多条邀请，一条邀请只兑换一次。

### 域类型

```go
type ProjectInvite struct {
    ID                 string
    TenantID           string
    Role               Role
    TokenHash          [32]byte
    CreatedByAccountID string
    CreatedAt          time.Time
    ExpiresAt          time.Time
    UsedAt             *time.Time
    UsedByAccountID    string
    RevokedAt          *time.Time
}

type InviteStatus string
const (
    InviteActive  InviteStatus = "active"
    InviteUsed    InviteStatus = "used"
    InviteExpired InviteStatus = "expired"
    InviteRevoked InviteStatus = "revoked"
)

func (i ProjectInvite) Status(now time.Time) InviteStatus
func NewInviteToken() (string, error) // inv_ + 32 字节 hex；失败直接返回错误，不降级到可预测值
```

### 服务（`internal/service/account`）

```go
type CreateInviteInput struct {
    Actor     domain.Principal
    TenantID  string
    Role      domain.Role
    ExpiresAt time.Time // 零值表示默认 7 天
}

type CreatedInvite struct {
    Invite domain.ProjectInvite
    Token  string // 明文，只在创建响应出现一次
    Path   string // "/__invite/<token>"，前端拼 origin
}

type InvitePreview struct {
    Invite        domain.ProjectInvite
    Tenant        domain.Tenant
    AlreadyMember bool
}

func (s *Service) CreateInvite(ctx context.Context, in CreateInviteInput) (CreatedInvite, error)
func (s *Service) ListInvites(ctx context.Context, actor domain.Principal, tenantID string) ([]domain.ProjectInvite, error)
func (s *Service) RevokeInvite(ctx context.Context, actor domain.Principal, tenantID, inviteID string) error

// UpdateInviteRole 修改尚未使用的邀请的角色。
func (s *Service) UpdateInviteRole(ctx context.Context, in InviteRoleInput) error
func (s *Service) PreviewInvite(ctx context.Context, actor domain.Principal, rawToken string) (InvitePreview, error)
func (s *Service) RedeemInvite(ctx context.Context, actor domain.Principal, rawToken string) (domain.ProjectInvite, error)
```

不变量：

- `CreateInvite` / `ListInvites` / `RevokeInvite` / `UpdateInviteRole` 要求 actor 在该项目至少是 owner（平台管理员对任意启用项目等价于 owner）。创建时项目必须存在、未归档且未停用。已发放的邀请可在被兑换前改角色；兑换后改角色走成员管理，以保留最后一个 owner 的保护。
- `CreateInvite` 的 `ExpiresAt` 为零时取默认 TTL；不晚于 `now` 返回 400；晚于 `now+MaxTTL` 返回 400。`role=owner` 且 actor 不是平台管理员返回 403。
- 兑换只接受账号主体（`AccountID` 非空）；管理 Token 没有账号身份，返回 403。账号被禁用、项目被停用或已归档都拒绝。
- 兑换在**一个事务**里完成：`UPDATE ... WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > now` 抢占、写/升级 `project_members`、更新账号级与（有会话时）会话级 `current_tenant_id`。任一失败整体回滚，邀请不会被白白烧掉。
- 角色只升不降：已有 owner 兑换 viewer 邀请仍是 owner；已有 viewer 兑换 editor 邀请升为 editor。SQL 用 rank 比较，避免读到旧角色后覆盖并发变更。
- 已使用且 `used_by_account_id == actor.AccountID` 时兑换是幂等的：**只在成员关系仍然存在时**切换当前项目并成功返回，绝不重建成员关系；兑换后被移出项目的人再打开旧链接返回“邀请无效或已过期”。其他账号再用同样失效。
- `PreviewInvite` 不消费、不要求登录；已使用且属于当前账号、项目未停用且仍是成员时返回 `AlreadyMember=true`，其余 inactive 情况统一返回 `ErrNotFound`，避免泄露“这条邀请曾经存在”。
- 兑换成功后清空主体缓存，下一个请求立即看到新成员关系与当前项目。

### 存储端口

```go
CreateInvite(ctx context.Context, inv domain.ProjectInvite) error
GetInviteByHash(ctx context.Context, hash [32]byte) (domain.ProjectInvite, error)
GetInvite(ctx context.Context, id string) (domain.ProjectInvite, error)
ListInvites(ctx context.Context, tenantID string) ([]domain.ProjectInvite, error)
RevokeInvite(ctx context.Context, tenantID, id string, at time.Time) error
UpdateInviteRole(ctx context.Context, tenantID, id string, role domain.Role) error
RedeemInvite(ctx context.Context, in RedeemInviteInput) error
PurgeInvitesBefore(ctx context.Context, before time.Time) (int64, error)
```

`RedeemInviteInput` 含 `InviteID`、`TenantID`、`Role`、`AccountID`、`SessionID`、`Now`。事务内的成员写入使用 SQL 公共子集：

```sql
INSERT INTO project_members (account_id, tenant_id, role, created_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(account_id, tenant_id) DO UPDATE SET
  role = CASE
    WHEN (CASE excluded.role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 ELSE 0 END)
       > (CASE project_members.role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 ELSE 0 END)
    THEN excluded.role ELSE project_members.role END
```

### 管理路由（`internal/transport/admin`，要求该项目 owner）

| 方法 | 路径 | body / query | 行为 |
|------|------|--------------|------|
| GET | `/admin/tenants/{id}/invites` | — | 返回 `{records:[{id,role,status,createdAt,expiresAt,usedAt,usedByAccountId,revokedAt}],total}`；不含 token/hash |
| POST | `/admin/tenants/{id}/invites` | `{role, expiresAt? , expiresIn?}` | 创建；`expiresAt` 为 RFC3339，`expiresIn` 为 Go duration（如 `24h`）；同时给时以 `expiresAt` 为准；返回 `{id,tenantId,role,path,createdAt,expiresAt}` |
| POST | `/admin/tenants/{id}/invites/{inviteId}/revoke` | — | 撤销；已撤销幂等成功；已使用返回 409；不存在 404 |
| POST | `/admin/tenants/{id}/invites/{inviteId}/role` | `{role}` | 修改未使用邀请的角色；只有 `active` 可改，其余状态 409；owner 角色仍限平台管理员 |
| GET | `/admin/tenants/{id}/member-candidates` | `query` | 成员候选搜索，见 `accounts.md`；要求 owner |

- 审计：`admin.invites.create`、`admin.invites.revoke`，字段含邀请 ID、项目、角色、过期时间、结果；**不含 token 明文或 hash**。
- 创建响应里的 `path` 是相对路径，前端用 `location.origin + path` 生成可复制链接；服务端不拼公网域名，避免配置漂移。

### 落地页与兑换（`internal/transport/auth`）

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/__invite/{token}` | 不消费；渲染自带样式的确认页（项目名、角色、有效期）。已登录且已是成员显示“进入项目”；未登录按登录模式跳转或提示 |
| POST | `/__invite/{token}` | 认证后兑换；成功 303 到 `/`；失败渲染同一页面的错误态 |

- 页面头：`Cache-Control: no-store`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'`。不引外部资源。
- **跨站提交防护**：POST 校验 `Sec-Fetch-Site`（只接受 `same-origin`/`none` 或缺省）与 `Origin`（存在时必须与 `Host` 同源）。零信任模式依赖网关注入的身份头而不是会话 cookie，SameSite 保护不到，这一步是必需的。Chrome 对同源 HTML 表单提交（导航型 POST，含确认页的 JS 自动提交）会发 `Origin: null`，因此只有同时带 `Sec-Fetch-Site: same-origin` 的 `null` 才放行；沙箱 iframe 与跨源重定向也会发 `null`，不能无条件信任。跨站表单要么带真实来源，要么 `Sec-Fetch-Site: cross-site`，仍被拒绝。2026-10-24 修复前这里把 `null` 当非法来源，导致浏览器里的邀请确认与设备授权提交必然失败。
- **GET 绝不消费**：聊天工具会预抓取链接，GET 消费会让链接在用户点击前失效。POST 才兑换；确认页可用 JS 自动提交（预抓取不执行 JS），并保留“加入项目”按钮兜底。
- 未登录时：账密模式 303 到 `/__login?next=/__invite/<token>`；零信任必选模式由网关先行认证，通常不会走到这里。`next` 只接受以单个 `/` 开头、不以 `//` 或 `/\` 开头、且不含控制字符的站内路径，其他值回落到 `/`。
- 账密模式的会话 cookie 是 `SameSite=Strict`：从 IM/邮件点链接属于跨站顶层导航，已登录用户也会被要求重新登录一次；零信任入口不受影响。
- 登录表单把 `next` 放进隐藏字段；登录成功 303 到 `next`，失败保留 `next` 重新渲染登录页。JSON 登录不改变既有响应形状。
- 失效统一文案“邀请无效或已过期”，不区分不存在、过期、撤销、被他人使用；已登录的本人可看到“你已加入该项目”。
- 提示：零信任网关不要给 `/__invite` 配白名单，否则请求不会带 `username`，而 dev/prod 已关闭账密登录。

### 审计与日志脱敏

- 兑换写 `auth.invite.redeem`：邀请 ID、项目、角色、用户名、来源 IP、成功/失败；不含 token。
- 访问日志与 panic 日志对 `/__invite/<token>` 脱敏为 `/__invite/***`，否则 token 会进入日志。指标标签使用 ServeMux 的路由模板 `/__invite/{token}`，本身低基数且不含 token。
- 邀请列表、审计、错误消息、结构化日志都不出现 `token` 或 `token_hash`。

### 清理

`Bootstrap` 调用 `PurgeInvitesBefore(now-30d)`，只删除 `expires_at < cutoff` 的邀请；活跃邀请不受影响。清理失败只记 WARN，不阻塞启动。

## 错误与边界情况

- 无效 token、过期、撤销、被他人使用：`ErrNotFound`，HTTP 404/403 的落地页统一文案；管理列表仍能看到历史状态。
- 项目停用或归档：创建返回 403；兑换返回 403/404（落地页统一“邀请无效或已过期”），不写成员。归档项目即使因历史脏行处于 `disabled=false`，也会先被 `Archived()` 拒绝。
- 账号禁用：兑换 403；已存在的成员关系不变，重新启用后仍生效。
- 并发兑换：事务抢占只有一条成功，其余返回失效；成员 upsert 是幂等的，不会产生重复行。
- 角色降级：邀请永远不降级已有成员；要降级走成员管理接口（受“不能自降/最后 owner”约束）。
- 管理 Token：可以创建/撤销邀请（平台管理员等价），不能兑换（没有账号身份）。
- 已使用邀请重复点击：本人幂等成功并切到项目；其他人失败。
- 过期时间边界：`expires_at == now` 视为过期；创建时 `expires_at <= now` 返回 400。
- 网关白名单、反向代理改写路径导致 token 丢失：落地页返回统一失效文案；不在客户端重试。

## 测试与验证

- store 单测：token 哈希查找、唯一约束、`RedeemInvite` 并发/抢占、成员升级不降级、事务失败回滚、`PurgeInvitesBefore`。
- service 单测：TTL 默认/上限/过去时间、owner 角色仅平台管理员、列表/撤销状态、`PreviewInvite` 不消费、本人幂等、管理 Token 不能兑换。
- 黑盒 e2e（真实 SQLite + 真实 HTTP）：创建 → GET 不消费 → POST 兑换 → 二次失败；过期、撤销、停用项目、禁用账号；并发兑换仅一成功；owner/viewer 权限反例；审计不含 token；访问日志脱敏。
- 浏览器：确认页样式、未登录跳转与 `next` 回跳、加入后落地到项目、二次打开提示、移动窄屏可读。

## 参考来源

- `docs/module/accounts.md`：账号、成员、角色矩阵、会话与 `Principal`。
- `docs/module/admin.md`：`/admin/*` 的鉴权包装与审计。
- `internal/transport/auth/login.go`、`page.go`：登录页、303、cookie 与错误页样式。
- `internal/service/identity/identity.go`、`internal/domain/token.go`：随机 token 与 SHA-256 查找的先例；邀请只在库里存哈希，明文只在创建响应出现一次。
- OWASP 邀请/重置链接建议：高熵、短有效期、单次使用、不在日志中出现、GET 不产生副作用。

## 实现记录

See the public version-control history.
