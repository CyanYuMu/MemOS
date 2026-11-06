---
module: zerotrust
packages:
  - internal/platform/zerotrust
status: implemented
---

# zerotrust

校验零信任 Web 网关注入的 `X-Zt-Authorization` JWT，把「网关已经认证过的用户」变成一个受信任的 `domain.ExternalIdentity`。它只解决「你是谁」，不解决「你能做什么」——授权仍由 [accounts](accounts.md) 的成员关系与角色负责。

## 职责与边界

- 负责：JWT 的 HS256 验签、`exp`/`iat` 时效校验、`username`/`name`/`email` 提取、错误分类（无效 token / 合法但无身份）。
- 不负责：读 cookie、发会话、查账号、判权（accounts）；不负责解析请求体与路由（transport）。
- 不信任请求里除该 header 之外的任何身份信息；`X-Zt-Authorization` 由网关生成，业务侧必须校验后才能采信。
- 只在 HTTP 入口生效。集群内直连、`port-forward`、MCP/REST 的 Bearer 链路都不经过网关，也不应依赖本模块。

## 依赖

- 只依赖标准库（`crypto/hmac`、`crypto/sha256`、`encoding/base64`、`encoding/json`、`time`）与 `internal/domain`（`ExternalIdentity`、typed errors）。
- 被 `internal/transport/httpx`（中间件）与 `internal/app`（构造 Verifier）依赖；`account` 服务不 import 本包，只接收 `domain.ExternalIdentity`。

## 契约

```go
const HeaderName = "X-Zt-Authorization"

var ErrInvalidToken = errors.New("zerotrust: invalid token")

type Verifier struct{ /* secrets、时钟、允许的时钟偏移 */ }

func New(secrets []string) (*Verifier, error)
func NewWithClock(secrets []string, now func() time.Time, leeway time.Duration) (*Verifier, error)
func (v *Verifier) Verify(token string) (domain.ExternalIdentity, bool, error)
```

不变量：

- secrets 至少一项；每项 ≥16 字节，否则启动失败。重复项自动去重。
- 逐个 secret 验签，任一匹配即通过；轮换期可同时配置新旧两个，旧 token 在 `exp` 内仍然有效。
- 只接受 `alg=HS256`；`alg=none`、其他算法、段数不为 3、base64 非法一律 `ErrInvalidToken`。
- 签名比较用 `hmac.Equal`（常量时间），不按内容短路。
- `exp` 必须存在；`now > exp+leeway` 拒绝。`iat` 存在时不得晚于 `now+leeway`，否则拒绝。
- `username` 为空时返回 `(零值, false, nil)`：网关白名单命中的请求没有用户身份，不能当作已登录。
- `name`/`email` 受 UUAP 半账号限制可能为空，调用方自行降级（显示名回退为 username，邮箱留空）。
- 单次接受的最大 token 长度 8 KiB，超限直接拒绝。

配置（`internal/config`）：

| 字段 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| Auth.ZeroTrust.Enabled | `MEMOSD_ZEROTRUST_ENABLED` | `false` | 打开 header 校验与外部身份认证 |
| Auth.ZeroTrust.Required | `MEMOSD_ZEROTRUST_REQUIRED` | `false` | 为 true 时只允许外部身份或管理 Token，本地会话不再单独通过 |
| Auth.ZeroTrust.AutoCreate | `MEMOSD_ZEROTRUST_AUTO_CREATE` | `true` | 首次出现的 username 自动建账号（无口令） |
| Auth.ZeroTrust.Secrets | `MEMOSD_ZEROTRUST_SECRET` | 空 | 逗号分隔的 JWT Secret 列表；Enabled 时至少一项且每项 ≥16 字节。轮换期可同时填新旧两个 |
| Auth.ZeroTrust.AllowedUsers | `MEMOSD_ZEROTRUST_ALLOWED_USERS` | 空 | 逗号分隔的 username 白名单；空表示不限制 |
| Auth.AllowPassword | `MEMOSD_AUTH_ALLOW_PASSWORD` | `true` | 为 false 时 `/__login` 返回 403 |

Transport 侧（`internal/transport/httpx`）：

- `ZeroTrust(verifier)` 中间件：header 缺失 → 直接放行（由后续鉴权决定 401）；header 存在但无效 → 401 + 统一错误信封；有效 → `domain.ExternalIdentity` 放进 request context。
- `CredentialsOf` 从 context 取出外部身份并填入 `domain.Credentials.External`，`account.Authenticate` 优先走外部身份分支。
- `LoggedOutGate` 中间件（挂在 `ZeroTrust` **外面**）：`/__logout` 写入会话级 cookie `memos_logged_out=1` 后，所有请求在 `/__relogin` 之前都被挡住——导航请求返回内置「已退出登录」页（含重新登录按钮），接口请求返回 401。
  为什么需要它：网关每个请求都会重新注入身份，UUAP 会话往往还没失效，只清本地会话时用户会在下一个请求被静默登回来（生产实测就是“点了退出马上又登录”）。`/__relogin` 清掉标记，之后身份仍由网关决定；标记不设 MaxAge，关浏览器即消失。

## 威胁模型与密钥轮换


- **不做 jti / 重放防护**：token 是逐请求签发的身份断言，不绑定 method/path/body；拿到 token 的人可以直接构造任意请求，去重只能拦住“完全相同的请求”。且 `iat` 是秒级、claims 固定，同一用户同秒的多个请求可能签出完全相同的 JWT，单次使用会误伤正常并发。缓解方向在传输层（源站目前是 HTTP，后续收敛为 HTTPS/网络隔离）和缩短 `exp`，不在业务侧。
- **不做热加载**：Secret 通过 env 注入，轮换是低频安全事件（控制台没有自助入口，需联系零信任团队）。k8s Secret 挂载更新有分钟级延迟，滚动重启更直接。

轮换流程：

1. 向零信任团队确认新 Secret 能否提前提供，以及切换时是否新旧 Secret 同时签发。
2. 能提前提供：`MEMOSD_ZEROTRUST_SECRET=旧,新` → 滚动重启（旧 token 仍可验）→ 网关切换 → 确认旧 Secret 无命中 → 改回单个新 Secret → 再滚动重启。
3. 不能提前提供：网关切换后立即把 Secret 改成 `旧,新` 并滚动重启；切换完成到重启完成之间会有短暂 401。
4. 若网关切换时会双签，旧 Secret 至少保留到旧 token 全部过期（`exp + leeway`）后再删。

## 错误与边界情况

- 网关「拦截模式」才会注入带 `username` 的 JWT；Bypass 模式或命中白名单时 username 为空，按未登录处理。
- 网关每个域名一个 JWT Secret，Secret 泄露等于任何能访问后端的人都能伪造身份；只从 Secret 注入，不落日志、不进 `Redacted()`。
- 服务端与网关时钟偏移超过 leeway（默认 1 分钟）会把有效请求判为过期；集群节点需开启 NTP。
- header 存在但格式错误时**不降级到本地会话**：显式 401 比静默回退安全，避免网关侧会话失效后仍能用旧 cookie 访问。
- 外部主体没有会话：`SessionID` 为空，`SwitchProject` 写账号级 `current_tenant_id`（`accounts` 表），下次请求由 `principalFor` 读回；不存在会话与账号级两套规则冲突。
- 直连（不经网关）的请求没有 header，`Required=true` 时一律 401；本地开发与集群内调试使用账密登录并保持 `Required=false`。

## 测试与验证

- 单元测试：有效 token、过期、签名错、`alg=none`、段数错误、缺 `exp`、`username` 为空、超大 token、时钟偏移边界。
- transport 单元测试：无 header 放行、有效 header 填充 `Credentials.External`、无效 header 401。
- app e2e：带签名 JWT 访问 `/api/auth/me` 首次自动建号并返回主体；多项目外部主体切换项目后 `currentProjectId` 落库并保持；`/api/meta` 的 `authMode` 为 `external`；`Required=true` 时直接创建的合法会话 cookie 也不能通过。

## 参考来源


## 实现记录

See the public version-control history.
