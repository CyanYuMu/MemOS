---
module: admin
packages:
  - internal/transport/admin
status: implemented
---

# admin

控制面自己的运维入口：租户、Token、账号与项目成员的管理。凭据是管理 Token（平台管理员等价的应急通道）或控制台登录会话；与租户 Token 完全分离。Dashboard 的“项目/密钥”页面映射到同一组 service 语义，但协议外壳不同。

## 职责与边界

- 负责：`/admin/*` 的 HTTP 编解码、凭据解析与按角色授权（规则见 `accounts.md`）、租户增改查、Token 签发/列表/吊销、账号与项目成员管理、审计。
- 不负责：租户 Token 的解析（identity 的 `Authenticate`）、记忆数据（memory/upstream）、Dashboard 路由（dashboard 模块）。管理 Token 不能调用记忆接口。

## 依赖

- `internal/service/identity`（`IssueToken`、`GetToken`、`ListTokens`、`RevokeToken`）与 `internal/service/account`（经 `Accounts` 接口：`Authenticate`、账号与成员管理）。
- `internal/transport/auth` 的 `Rule`：每条路由声明最低角色，授权统一在 `wrap` 里完成。
- `internal/service/audit`、`internal/transport/httpx`。
- 租户规则（命名、保留前缀、upsert 保留原值与归档状态）在 [tenant](tenant.md) 服务里，本包只做编解码；任务排查走 store 直接满足的窄接口：

```go
type Tenants interface { // service/tenant 实现
    Get(ctx context.Context, id string) (domain.Tenant, error)
    List(ctx context.Context) ([]domain.Tenant, error)
    Upsert(ctx context.Context, in tenant.UpsertInput) (domain.Tenant, error)
}

type Jobs interface { // service/jobs 实现，重放安全的判定在服务里，见 jobs.md
    List(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error)
    Retry(ctx context.Context, id string, force bool) (domain.Job, error)
}
```

## 契约

```go
type Deps struct {
    Tenants  Tenants
    Jobs     Jobs
    Identity *identity.Service
    Accounts Accounts // Authenticate 与账号/成员管理，由 service/account 满足
    Audit    *audit.Recorder
    Logger   *slog.Logger
}

func New(deps Deps) *Server
func (s *Server) Handler() http.Handler // 挂到 /admin/
```

鉴权：`Authorization: Bearer <admin-token>`（平台管理员等价）或会话 cookie；缺失/无效返回 401 信封 `{code:40100,data:null,message:"登录已过期，请重新登录"}`，没有对应项目角色返回 403。租户 Token 不能用于 `/admin/*`。管理 Token 未配置时 Bearer 路径不可用，会话路径不受影响。

| 方法 | 路径 | 最低要求 | body | 行为 |
|------|------|----------|------|------|
| GET | `/admin/tenants` | 平台管理员 | — | `{code:0,data:{records:[Tenant],total:n}}`，按 id 升序 |
| POST | `/admin/tenants` | 平台管理员 | `{id,user_id,name,disabled,description?,isMultiView?}` | 创建或更新；`id`/`user_id` 必填，`disabled` 缺省 false；未提供的 `description`/`isMultiView` 保留原值；已归档项目保持停用（`archived_at` 与 `disabled` 不会分离） |
| GET | `/admin/tenants/{id}/tokens` | 该项目 viewer | — | 该租户的 Token 元数据（含前缀、标签、最后使用、吊销时间），**不含哈希与明文** |
| POST | `/admin/tenants/{id}/tokens` | 该项目 editor | `{label}` | 签发新租户 Token（`user_id` 取租户当前值，创建者记入 `created_by_account_id`）；明文只在响应里出现一次 |
| POST | `/admin/tokens/{id}/revoke` | Key 所属项目 editor | — | 吊销；不存在返回 404（平台管理员）或 403（其他人），重复吊销幂等成功 |
| GET | `/admin/accounts` | 平台管理员 | query：`query`、`page`、`pageSize` | 按用户名/显示名过滤后分页 |
| GET | `/admin/accounts/{id}` | 平台管理员 | — | 单个账号；响应不含口令哈希 |
| POST | `/admin/accounts` | 平台管理员 | `{username,password,display_name,platform_admin}` | 创建账号 |
| POST | `/admin/accounts/{id}` | 平台管理员 | `{display_name,disabled,platform_admin}` 任选 | 更新；禁用会吊销该账号全部会话；不能禁用或降级最后一个启用的平台管理员（409）；不能自降平台管理员或自我禁用（409） |
| POST | `/admin/accounts/{id}/password` | 平台管理员 | `{password}` | 重置口令、吊销全部会话并清掉登录失败计数 |
| DELETE | `/admin/accounts/{id}` | 平台管理员 | — | 删除账号；不能删自己或最后一个启用的平台管理员；个人项目连同其成员/邀请/API Key/待执行任务一起删除，团队项目只移除成员关系，创建的 Key/邀请保留但清空创建者引用 |
| POST | `/admin/accounts/{id}/sessions/revoke` | 平台管理员 | — | 吊销该账号全部会话 |
| GET / POST | `/admin/tenants/{id}/members` | 该项目 owner | `{account_id 或 username, role}` | 列出/设置成员角色（幂等）；不能把自己降级为 viewer/editor；owner 可直接授予 owner（邀请链接的 owner 仍只允许平台管理员创建） |
| POST | `/admin/tenants/{id}/members/leave` | 该项目 viewer | — | 自助退出项目；最后一个 owner 不能直接退出，必须先转让 |
| POST | `/admin/tenants/{id}/members/{accountId}/remove` | 该项目 owner | — | 移除成员；最后一个 owner 不能被降级或移除（409）；不能移出自己（409） |
| GET | `/admin/tenants/{id}/member-candidates` | 该项目 owner | query：`query`（可带前导 `@`） | 返回最多 10 条 `{accountId,username,displayName,disabled,member}`；空 query 返回空列表 |
| GET / POST | `/admin/tenants/{id}/invites` | 该项目 owner | 见 [invites](invites.md) | 列出/创建邀请；创建响应含一次性明文 `path` |
| POST | `/admin/tenants/{id}/invites/{inviteId}/revoke` | 该项目 owner | — | 撤销邀请；不返回 token/hash |
| POST | `/admin/tenants/{id}/invites/{inviteId}/role` | 该项目 owner | `{role}` | 修改未使用邀请的角色；已使用/已撤销/已过期返回 409；owner 邀请仍只有平台管理员可以设置 |
| GET | `/admin/jobs` | 平台管理员 | query：`status`、`kind`、`tenant_id`、`limit`、`offset` | 异步任务只读查询（死信排查），`{code:0,data:{records:[Job],total:n}}`，按 `created_at` 倒序；`limit` 默认 50、上限 200；**不含 payload** |
| POST | `/admin/jobs/{id}/retry` | 平台管理员 | `{force?: bool}` 或 `?force=1` | 重放死信：`failed` → `pending`、`attempts` 归零、`last_error` 清空；只接受 `failed`（其余 409）；`last_error` 不在 `domain.ReplaySafe` 白名单时必须 `force`，否则 409 并说明「可能已写入上游」；返回 `{ok:true,job}` |

账号与成员路由的完整语义、角色矩阵与会话规则见 `accounts.md`。

不变量：

- Token 明文只在签发响应里自动返回；服务端同时保存 `tokens.raw` 供控制台按写权限单把复制，但 `/admin/*` 的列表与详情都不返回明文，也不返回 `hash` 字段。
- 租户 ID 只允许 `[A-Za-z0-9._-]{1,64}`，且不能为空；写入前校验，避免拼接注入。
- Token 的 `user_id` 始终取租户当前值，避免同一租户出现互相矛盾的身份。
- 所有写操作写一条 `domain.AuditRecord`（`Tool` 为 `admin.<动作>`，如 `admin.tenants.upsert`、`admin.accounts.create`），`UserID` 取 `Principal.Actor()`；`Input` 原样记录（超过单条 256 KiB 总量上限时按字段预算截断并留标记），其中 `password` 一律替换为 `***`，邀请 token 与 hash 一律不记录，响应不回显审计内容。
- 成员与账号的自我保护是服务端不变量：actor 不能把自己降级/移出项目，也不能撤销自己的平台管理员标记或禁用自己的账号；前端禁用按钮只是体验。
- 成功与失败都用 `httpx.Envelope`，错误码沿用 httpx 常量；上游不参与。

## 错误与边界情况

- 管理 Token 未配置（启动参数为空）：Bearer 请求一律 401，启动日志提示未启用该应急通道；账号会话不受影响。
- 租户不存在：查询/签发/吊销返回 404；更新不存在时按创建处理（与 `UpsertTenant` 一致），并在文档记录这一选择。新建项目的 `id` 不能以 `u-` 开头（400），已有的个人项目仍可被 upsert；对已归档项目 upsert 不会恢复它。
- 租户已禁用：仍允许查看与吊销 Token；签发 Token 被拒绝（403）。
- 请求体非法 JSON：400；非 JSON Content-Type 且有 body：415。
- 审计入队失败不影响响应；失败只记 WARN。

## 测试与验证

- `httptest`：管理 Token 校验（缺/错/租户 Token 拒绝）、租户列表与更新、Token 签发（明文只出现一次）、列表不含哈希、吊销、404/400/415。
- 账号、成员与角色矩阵由 `internal/app` 的黑盒 e2e 覆盖（真实 SQLite + HTTP），不在本包重复。
- 本地单元测试；第 3 层验证只做健康检查（管理入口不在 smoke 范围）。

## 参考来源

- `docs/module/identity.md` 的 Token 生命周期与 `Token` 字段。
- `internal/adapter/store/tenant.go`、`token.go` 的存储语义。
- `docs/ARCHITECTURE.md` 的“租户与身份”。

## 实现记录

See the public version-control history.
