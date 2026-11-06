---
module: tenant
packages:
  - internal/service/tenant
status: implemented
---

# tenant

项目（租户）注册表的用例层：创建团队项目、改名与改描述、启停、归档与恢复、运维 upsert。控制台与管理入口共用这一套规则，规则本身（命名、保留前缀、个人项目不能停用或删除、归档项目只能先恢复）落在 `domain.Tenant` 上。

## 职责与边界

- 负责：项目名称校验与 `u-` 保留前缀、同名预检查、创建者成为 owner（租户行与成员行同一事务）、管理 Token 创建项目时的 owner 解析、停用/归档/恢复的状态规则、运维 upsert 的“未提供字段保留原值”（含归档状态）、变更后清空鉴权主体缓存。
- 不负责：授权（`Principal.RequireCap` 与“只有 owner 能恢复归档项目”由 transport 先判定）、响应形状、会话级“切换当前项目”（`account.Service.SwitchProject`）、成员与邀请（`account`）、Token 签发（`identity`）、SQL（`store`）。
- 此前这些规则散在 `transport/dashboard/projects.go` 与 `transport/admin/server.go`，各自一份：`tenantIDPattern` 重复，`u-` 保留前缀只有控制台校验，运维 upsert 会丢掉归档状态。收口后 transport 不再持有租户仓储，`app` 只把本服务接给它们。

## 依赖

- 依赖 `internal/domain`。
- 以使用方定义的窄接口依赖存储与账号，不 import adapter 或 account：

```go
type Store interface {
    GetTenant(ctx context.Context, id string) (domain.Tenant, error)
    ListTenants(ctx context.Context) ([]domain.Tenant, error)
    UpsertTenant(ctx context.Context, t domain.Tenant) error
    CreateTenantWithOwner(ctx context.Context, t domain.Tenant, ownerAccountID string, role domain.Role) error
    ArchiveTenant(ctx context.Context, id string, at time.Time) error
    RestoreTenant(ctx context.Context, id string) error
}

type Accounts interface {
    ResolveAccount(ctx context.Context, idOrUsername string) (domain.Account, error)
}
```

## 契约

```go
func New(store Store, accounts Accounts, invalidate func()) *Service

func (s *Service) Get(ctx context.Context, id string) (domain.Tenant, error)
func (s *Service) List(ctx context.Context) ([]domain.Tenant, error)
func (s *Service) Create(ctx context.Context, actor domain.Principal, in CreateInput) (domain.Tenant, error)
func (s *Service) Update(ctx context.Context, id string, patch Patch) (domain.Tenant, error)
func (s *Service) Disable(ctx context.Context, id string) error
func (s *Service) Archive(ctx context.Context, id string) error
func (s *Service) Restore(ctx context.Context, id string) error
func (s *Service) Upsert(ctx context.Context, in UpsertInput) (domain.Tenant, error)
```

`invalidate` 在每次成功的写入后调用（`account.Service.InvalidatePrincipalCache`），权限变化不用等 10 秒缓存过期；可为 nil。

- Create：名称必填、符合 `domain.ValidTenantID`、不以 `domain.PersonalTenantPrefix` 开头、不与现有项目同名。账号主体自己建项目并成为 owner；管理 Token 没有账号身份，必须给 `OwnerUsername`，且该账号未禁用。只写注册表，不调上游。
- Update：先拒绝归档项目；停用个人项目被拒绝；改名沿用 `ValidTenantID`。`Patch` 里为 nil 的字段不动。
- Disable：回收项目（控制台“revoke”）。个人项目被拒绝；不检查归档状态。
- Archive：个人项目被拒绝；已归档保持第一次的时间。Restore：未归档返回冲突。
- Upsert（运维）：`id` 与 `user_id` 必填；`name` 缺省取 `user_id`；`disabled` 缺省 false，但已归档项目由 store 的 `normalizeArchived` 强制 `disabled=true`，upsert 只改元数据、不能把归档项目改回可用；`description` / `multiView` 未提供时保留原值；已存在项目的归档状态保留（归档项目不会被 upsert 悄悄恢复）。新建项目同样受保留前缀约束，已存在的个人项目仍可被 upsert（平台管理员停用个人项目的唯一入口）。

## 错误与边界情况

- 名称非法、保留前缀、同名、缺 owner、owner 已禁用：`domain.ErrInvalidInput`。
- 个人项目停用/删除、归档项目被改动、恢复未归档项目：`domain.ErrConflict`。
- 项目不存在：`domain.ErrNotFound`（来自 store）。
- 唯一约束冲突（上游 `user_id` 已被占用、预检查之后的并发同名创建）：`domain.ErrConflict` 经 store 透传；创建路径把并发同名映射回与预检查一致的 `ErrInvalidInput`。
- 读后写不是事务：并发的 Update 仍是最后写入者赢，与迁移前一致。

## 测试与验证

- `internal/service/tenant` 用内存假仓储覆盖：命名与保留前缀、管理 Token 的 owner 解析、归档/恢复/停用的状态规则、upsert 保留归档状态与原值、每次写入都触发 `invalidate`。
- `internal/domain` 覆盖 `ValidTenantID`、`IsReservedTenantID` 与四个 `Ensure*` 守卫。
- transport 的 golden 与 `internal/app` 的黑盒 e2e（个人项目、ACL、事务回滚）继续作为行为回归。

## 参考来源

- 迁移前的实现：`internal/transport/dashboard/projects.go`、`internal/transport/admin/server.go`（见本仓库提交历史）。
- 项目、个人空间与归档的既有约定：[accounts](accounts.md)、[admin](admin.md)、[dashboard-routes](dashboard-routes.md)。

## 实现记录

See the public version-control history.
