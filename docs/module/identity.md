---
module: identity
packages:
  - internal/service/identity
status: implemented
---

# identity

把 Bearer Token 解析成租户与调用方，并负责 Token 的签发、列出、改名、吊销和启动期静态租户引导。鉴权只按哈希查找；控制台可复制的明文是额外的 `tokens.raw` 字段（迁移 `0024`），只在项目写权限下按 id 单把下发。

## 职责与边界

- 负责：token 哈希与常量时间比较、`Authorization: Bearer` 解析、租户禁用与 Token 过期检查、管理 Token 与租户 Token 的隔离、静态租户/管理 Token 的幂等引导、`LastUsedAt` 的节流更新。
- 不负责：HTTP 头解析与状态码映射（transport）、Token 存储（store）、Tool 参数校验。工具参数里没有 `user_id`，identity 也不接受调用方自报身份。

## 依赖

- 依赖 `internal/domain` 与 `internal/platform/logging`。
- 以使用方定义的窄接口依赖存储，不 import adapter：

```go
type Store interface {
    GetTenant(ctx context.Context, id string) (domain.Tenant, error)
    UpsertTenant(ctx context.Context, t domain.Tenant) error
    EnsureTenant(ctx context.Context, t domain.Tenant) error
    CreateToken(ctx context.Context, t domain.Token) error
    UpsertToken(ctx context.Context, t domain.Token) error
    TokenByHash(ctx context.Context, hash [32]byte) (domain.Token, error)
    GetToken(ctx context.Context, id string) (domain.Token, error)
    ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
    RevokeToken(ctx context.Context, id string, at time.Time) error
    UpdateTokenLabel(ctx context.Context, id, label string) error // 改名，不动哈希/前缀/过期
    TouchToken(ctx context.Context, id string, at time.Time) error
}
```

## 契约

```go
type StaticTenant struct {
    Token  string
    UserID string
    CubeID string
}

type Service struct{ /* ... */ }

func New(store Store, static []StaticTenant, adminToken string, log *slog.Logger) *Service

// Bootstrap 幂等写入静态租户、租户 Token 与管理 Token。启动时调用一次。
func (s *Service) Bootstrap(ctx context.Context) error

// Authenticate 解析租户 Token，返回不含明文凭据的 Caller。
func (s *Service) Authenticate(ctx context.Context, raw string) (domain.Caller, error)

// AuthenticateAdmin 只接受管理 Token；租户 Token 一律拒绝。
func (s *Service) AuthenticateAdmin(ctx context.Context, raw string) (domain.Caller, error)

// IssueToken 生成新租户 Token，明文在这里返回。Token 永不过期。
// createdBy 是创建者账号 ID（管理 Token 调用时为空），只写入 Token.CreatedByAccountID 供审计，不参与鉴权。
func (s *Service) IssueToken(ctx context.Context, tenantID, label, createdBy string) (raw string, tok domain.Token, err error)

// IssueTokenWithExpiry 签发可过期 Token；expiresAt 为 nil 表示永不过期。
// 过期时间只写在 Token 元数据里，不给调用方暴露存储细节。
func (s *Service) IssueTokenWithExpiry(ctx context.Context, in TokenIssueInput) (raw string, tok domain.Token, err error)

// GetToken 按 ID 读取 Token 元数据（含 Raw；transport 用它解析项目与按需返回明文）。
func (s *Service) GetToken(ctx context.Context, id string) (domain.Token, error)

func (s *Service) ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
func (s *Service) RevokeToken(ctx context.Context, id string) error
func (s *Service) RenameToken(ctx context.Context, id, label string) error
```

不变量：

- 查找按 `SHA-256(raw)` 做；取回后再用 `crypto/subtle.ConstantTimeCompare` 比较哈希，避免按内容短路。
- Token 被吊销（`RevokedAt != nil`）、已过期（`ExpiresAt` 非 nil 且 `!now.Before(*ExpiresAt)`）、租户 `Disabled`、kind 不匹配时分别返回 `ErrUnauthorized` / `ErrForbidden`；错误消息不区分“不存在”“已过期”与“已吊销”（避免枚举）。`ExpiresAt == nil` 表示永不过期，静态 Token 与管理 Token 都是这种。
- 鉴权缓存（10s）只缓存元数据，命中后仍要用当前时钟再检查一次过期：缓存窗口内跨过过期点的 Token 同样拒绝；`RevokeToken` 与 `RenameToken` 会整体清空缓存，吊销/改名立即生效。
- 管理 Token 的 `Token.TenantID` 为空；`Authenticate` 拒绝 `TokenKindAdmin`，`AuthenticateAdmin` 拒绝 `TokenKindTenant`。
- 引导 ID 是确定性的（`static-<hash 前 12 hex>`、`static-admin-<hash 前 12 hex>`），重复启动不会产生重复记录。`UpsertToken` 保留已有的 `revoked_at`：管理员吊销静态 Token 后，只要明文仍在配置里，重启也不会复活；要恢复必须轮换配置中的 Token。
- `Bootstrap` 用 `EnsureTenant` 只同步 `user_id`/`name`，保留已有的 `disabled`；禁用租户不会因重启恢复。静态租户的 `name` 以配置为准，每次启动会被配置中的 `UserID` 覆盖（Dashboard 改名不跨重启保留）；需要持久展示名时另加字段。
- 静态 Token 的展示名默认是「部署配置」（不再是内部术语 `bootstrap`）；`UpsertToken` 只在库里名字为空串时写入默认名，管理员改过的名字保留。同一租户配置多条静态 Token 时启动日志打 WARN。
- 静态 Token 与 `IssueTokenWithExpiry` 签发的 Token 都会把明文写入 `tokens.raw`，供控制台按 id 复制；管理 Token 不保存明文。历史行或旧网关导入的 Key `raw` 为空，`keys/copy` 返回 409，不能把空串当明文下发。
- `LastUsedAt` 最频繁每分钟更新一次（进程内节流），更新失败只记 DEBUG，不影响鉴权。
- 明文不进入日志、不进入 `Caller`。

## 错误与边界情况

- 空 token、非 `Bearer` 前缀由 transport 在调用前拒绝；identity 对空 token 也返回 `ErrUnauthorized`。
- 存储不可用返回包装后的内部错误，transport 映射为 503/500，而不是 401（避免把故障伪装成凭据错误）。
- `IssueToken` 时租户不存在或已禁用返回 `ErrNotFound` / `ErrForbidden`。过期 Token 的过期判定以服务端时钟为准，不读取客户端时间。
- `UpsertToken` 只同步静态 Token 的归属，不清除也不复活已有的 `expires_at`；引导生成的静态 Token 本身写空串（永不过期）。

## 测试与验证

- 用内存假 Store：正确 token 通过、错误 token 401、吊销后 401、禁用租户 403、admin/tenant 互斥。
- 过期：未过期 Token 可用；到过期点（含相等）后 401；缓存命中跨过过期点仍拒绝；静态 Token 无 `ExpiresAt` 时永不过期。
- 引导幂等：连续两次 `Bootstrap` 只有一份租户与 Token。
- 哈希：库里拿到的 `Token.Hash` 等于 `domain.HashToken(raw)`；签发与静态引导的 Token 带 `Raw`，管理 Token 不带明文；改名只改 `Label`，哈希、前缀与过期时间不变。
- 本地单元测试。

## 参考来源

- `AGENTS.md`：“身份只来自 Bearer token 到租户的映射，MCP 工具参数里没有 user_id”；鉴权用哈希，控制台可复制明文只按写权限单把下发。

## 实现记录

See the public version-control history.
