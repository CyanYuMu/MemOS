---
module: httpx
packages:
  - internal/transport/httpx
status: implemented
---

# httpx

入站 HTTP 的共享基础件：中间件链、请求 ID、panic 恢复、访问日志、请求体上限、统一信封、鉴权上下文、健康检查、服务端超时。只放与业务无关的东西。

## 职责与边界

- 负责：`http.Handler` 包装、`context` 中的请求 ID 与调用方、响应写入（JSON/信封）、请求体解码与大小限制、`http.Server` 构造参数、健康检查处理器。
- 不负责：路由与协议（各 transport 模块）、身份查询（identity）、错误码业务映射（各 transport 自己把 `domain.Error` 映射成状态码）。

## 依赖

- 依赖 `internal/domain`（`Caller`、错误哨兵）、`internal/platform/logging` 与 `internal/platform/metrics`（`HTTPObservation`）。
- 被 `transport/{mcp,rest,dashboard,admin}` 与 `internal/app` 使用。

## 契约

### 同源表单提交

```go
func SameOriginPost(r *http.Request) bool
```

- 供 `/__invite/{token}`、`/__device` 这类“浏览器确认页 + 原生表单 POST”复用，避免两处各写一份后口径漂移。
- 规则：`Sec-Fetch-Site` 只接受 `same-origin`/`none`/缺省；`Origin` 缺省时放行（非浏览器客户端通常不带这些头，仍要携带有效身份，不构成浏览器 CSRF）；`Origin: null` **只有同时带 `Sec-Fetch-Site: same-origin` 才放行**——Chrome 对同源导航型表单提交会发 `null`，但沙箱 iframe 与跨源重定向也会发 `null`，无条件放行会打开 CSRF 缺口；有具体来源时必须与 `Host` 或 `X-Forwarded-Host` 第一段同源。

### 中间件

```go
func Chain(h http.Handler, mws ...func(http.Handler) http.Handler) http.Handler
func RequestID(next http.Handler) http.Handler
func Recover(log *slog.Logger) func(http.Handler) http.Handler
func AccessLog(log *slog.Logger, opts AccessLogOptions) func(http.Handler) http.Handler
func ClientIP(next http.Handler) http.Handler
func MaxBody(limit int64) func(http.Handler) http.Handler
func CORS(origins []string) func(http.Handler) http.Handler
func ZeroTrust(v ExternalVerifier, header string) func(http.Handler) http.Handler
func MethodNotAllowedJSON(next http.Handler) http.Handler
func SPA(dir string) (http.Handler, error)

type AccessLogOptions struct {
    Role    string
    Metrics Observer // ObserveHTTP(metrics.HTTPObservation)；nil 表示不记录
}
```

- 请求 ID：接受 `X-Request-Id`，只允许 `[A-Za-z0-9._-]` 且长度 8..128，否则生成 16 字节随机 hex；写入 `context` 与响应头 `X-Request-Id`。
- `Recover`：panic 时记 `error` 日志（含 `request_id`、`method`、`path`，不含 body），返回信封 `{code:50000,data:null,message:"内部错误"}`，状态码 500。
- `AccessLog`：响应写出后记一行：`method`、`path`（不含 query，邀请路径脱敏为 `/__invite/***`）、`status`、`duration_ms`、`request_id`、`remote_ip`，有调用方时带 `tenant_id`。**不记请求体与响应体**；同一行顺带上报 HTTP 指标。
- `ClientIP`：把 TCP 对端 IP 放进 context，供审计与日志使用；不信任 `X-Forwarded-For`。
- `MaxBody`：`http.MaxBytesReader`，超限时解码返回 `ErrBodyTooLarge`，由调用方回 413。
- `CORS`：只放通 `MEMOSD_CORS_ORIGINS` 列出的完整来源；为空表示不放通跨域，`*` 在配置校验期被拒绝。
- `ZeroTrust`：校验 `X-Zt-Authorization`。header 缺失直接放行（是否登录由后续鉴权决定）；header 存在但验签失败/过期返回 401 信封，不降级到会话 cookie；有效时把 `domain.ExternalIdentity` 放进 request context，供 `CredentialsOf` 填入 `Credentials.External`。只在 console 角色挂载，MCP/REST 不经过零信任网关。
- `MethodNotAllowedJSON`：把 Go 1.22 ServeMux 的纯文本 405 换成 `{code:40500,...}` 信封，保留 `Allow` 头。
- `SPA`：同源托管前端产物。命中文件直接返回；未命中且无扩展名（前端路由）回退 `index.html`；带扩展名的未命中返回 404；只接受 GET/HEAD。`index.html` 与回退响应 `Cache-Control: no-store`，其余静态文件 `max-age=3600`。构造时校验目录存在且含 `index.html`，不满足直接报错，避免启动后才发现页面打不开。

### 信封与响应

```go
type Envelope struct {
    Code    int    `json:"code"`
    Data    any    `json:"data"`
    Message string `json:"message"`
}

func OK(w http.ResponseWriter, data any)
func Fail(w http.ResponseWriter, status, code int, message string)
func WriteJSON(w http.ResponseWriter, status int, v any)
func NotFoundJSON(w http.ResponseWriter)
func SetRetryAfter(w http.ResponseWriter, window time.Duration)
```

- `OK` = `{code:0,data:...,message:"success"}`，HTTP 200。
- 错误信封的 `code` 用 Dashboard 的 cloud 错误码：`40000` 参数、`40001` 不存在、`40100` 未登录、`40300` 无权限、`40500` 方法不允许、`40900` 冲突、`41300` 请求体或正文超过上限（HTTP 413）、`42900` 限流、`50000` 内部错误、`50100` 未实现、`50143` 上游拒绝、`50300` 能力未启用或上游不可用；`data` 固定 `null`。
- Content-Type 一律 `application/json; charset=utf-8`。
- `NotFoundJSON` = 404 + `{code:40001,data:null,message:"接口不存在"}`。各角色 mux（dashboard/admin/auth）与 MCP 外层 mux 都注册 `"/"` 兜底，未知路径不再落到 Go 默认的 text/plain `404 page not found`；Go 1.22 会优先匹配更具体的 pattern，已有路由、`/healthz`、`/readyz`、`/metrics` 与 SPA 的 `/` 回退都不受影响。`consoleHandler` 在未配置 `WebDir`（`a.web == nil`）时也用 `NotFoundJSON` 兜底 `/`，否则 API 请求之外的未知路径会变成纯文本。REST 面的兜底**不用**这个信封：它的其它错误都是 `{status,message}`，未知路径也用同一形状。
- 已注册路径的方法不匹配由 `MethodNotAllowedJSON` 包装成 `{code:40500,data:null,message:"方法不允许"}` 并保留 `Allow` 头；REST 面自己的 `wrap` 返回 `{status:"error",message:"只接受 POST"}`。
- `SetRetryAfter` 把限流窗口写成 `Retry-After`（秒，向上取整，至少 1）。窗口是**最长需要等待多久的上限**，不是精确剩余时间；`window<=0`（调用方没有窗口信息）时不设置该头，保持旧行为。目前只有登录失败节流的 429（`transport/auth`）调用它；上有限流导致的 429 不带该头，因为控制面拿不到上游的真实恢复时间。

### 鉴权头

```go
func BearerToken(header string) string
func CredentialsOf(r *http.Request) domain.Credentials
func WithClientIP(ctx context.Context, ip string) context.Context
func ClientIPFrom(ctx context.Context) string
```

- 只做解析：`Authorization: Bearer <token>` 的方案名不区分大小写，两边空白忽略。
- `CredentialsOf` 收集 Bearer、会话 cookie（`memos_console_sid`）与 `ZeroTrust` 中间件放进 context 的外部身份，不做任何校验。
- 缺失、方案不对或 token 为空都返回空串；是否放行由调用方（mcp/rest）交给 identity 判定。
- 不记录、不回显 token。

### 请求体

```go
const DefaultMaxBody = 4 << 20
func String(value any) string
func Int(value any, def int) int
func Bool(value any, def bool) bool
func StringList(value any) []string
func DecodeJSON(r *http.Request, dst any) error // 返回 ErrBodyTooLarge / ErrBadJSON
func RequireJSON(r *http.Request) error         // 非 application/json 返回 ErrNotJSON
```

- 按字节读取后再 `json.Unmarshal`，避免分片解码把跨片的 UTF-8 汉字打成 `U+FFFD`。
- 未知字段不报错（前端会多传字段）；`null` body 视为空对象。
- 写接口要求 `Content-Type: application/json`：不是 JSON 时返回 415（CSRF 纵深防御），由各 transport 显式调用 `RequireJSON`。
- `String`/`Int`/`Bool`/`StringList` 是三个入站面共用的宽松取值：类型不符返回零值或默认值，字符串去首尾空白。

### 调用方上下文

```go
type callerKey struct{}
func WithCaller(ctx context.Context, c domain.Caller) context.Context
func CallerFrom(ctx context.Context) (domain.Caller, bool)
func WithRequestID(ctx context.Context, id string) context.Context
func RequestIDFrom(ctx context.Context) string
```

- `RequestID` 中间件同时写入 `httpx` 与 `platform/logging` 的 context 键：日志处理器只认后者，只写一个会导致业务日志缺少 `request_id`。
- 只接受 `^[A-Za-z0-9._-]{8,128}$` 的入站 `X-Request-Id`，否则生成随机 ID，避免把调用方输入原样写进日志与响应头。

### 健康检查

```go
type Check func(ctx context.Context) error
type Readiness struct{ /* 注册的检查 */ }
func (r *Readiness) Add(name string, c Check)
func (r *Readiness) Handler() http.Handler
func HealthHandler() http.Handler
```

- `GET /healthz`：进程存活即 200 `{"status":"ok"}`，不查依赖。
- `GET /readyz`：逐个跑检查（store ping、上游可达性等由 app 注册），全部通过 200 `{"status":"ready","checks":{...}}`，否则 503 并列出失败项（只写名字，不写错误详情，避免泄露地址）。

### 服务端

```go
func NewServer(addr string, h http.Handler, opts ServerOptions) *http.Server
type ServerOptions struct {
    ReadHeaderTimeout time.Duration
    ReadTimeout       time.Duration // 只限制请求体读取，不影响 SSE 响应
    IdleTimeout       time.Duration
    ShutdownTimeout   time.Duration
}
```

- `ReadHeaderTimeout` 与 `ReadTimeout` 必须 > 0；`ReadTimeout` 只覆盖请求体读取，不截断 SSE 响应；不设 `WriteTimeout`（`/mcp` 的 SSE 长连接会被截断），用 `IdleTimeout` 兜底。
- 不使用 `http.ListenAndServe*`；启停由 `internal/app` 负责。

## 错误与边界情况

- `DecodeJSON` 超限后先让 413 发出去再断开连接，避免客户端只看到连接重置。
- 访问日志不得记录 query string 与 header；token 只出现在 `Authorization`，不落日志。
- `Chain` 的顺序是“先注册先执行外层”，文档示例以 `RequestID → Recover → AccessLog` 为准。
- 未知路径统一返回 JSON 404 信封（REST 面为 `{status,message}`）；401/403 等错误信封不附带 `Retry-After`，只有 429 且调用方知道窗口时才带。

## 测试与验证

- 用 `httptest` 覆盖：请求 ID 透传与生成、panic 恢复、超限 413、非法 JSON 400、信封字段、Caller 上下文、`/readyz` 的通过/失败分支。
- 本地单元测试。

## 参考来源

- `AGENTS.md`：“Client 与 Server 都要显式超时；路由用标准库 ServeMux”。

## 实现记录

See the public version-control history.
## 2026-11-01 环境化接入地址（implemented）

部署以 `MEMOSD_PUBLIC_SERVICE_URL` 与 `MEMOSD_PUBLIC_CONSOLE_URL` 指定对外地址，两者同时为空表示外部接入未配置；配置时必须同时为不含凭据/query/fragment的绝对HTTP(S)地址。前端通过同源 `/runtime-config.js` 在模块加载前读取运行时值，镜像可跨环境使用；构建不再默认生产网关，合并分支不决定环境。缺配置时接入示例关闭并提示联系管理员，不回退生产。

安装命令收敛为一行curl下载当前环境网关的脚本。发布端按同一运行时配置为脚本注入下载基址、网关与密钥页默认值，保留调用者显式覆盖；脚本内容仍来自插件发布包，不修改安装算法。无配置的安装脚本返回503，静态包白名单不变。JS与脚本no-store/no-cache，JSON和Shell编码防止地址变为可执行内容，GET/HEAD语义一致。prod仅更新待发布清单与runbook，dev入口未确认时保持未配置。
