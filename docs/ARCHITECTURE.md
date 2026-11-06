# 架构

本仓库是 MemOS 的**控制面**：身份、租户、密钥、审计、聚合、配置，以及对外的 MCP 协议与管理后台后端。**数据面**（抽取、存储、检索、Dream）仍在 dumemos 的 memos-api，两边只通过 HTTP 对接，不共享代码，不直连对方的库。

这样拆的原因是 memos-api 是记忆存储与演化的地方，不该混进租户、鉴权、控制台聚合这些耦合；控制面出问题也不该拖垮写入链路。图谱与事件记忆/Dream 产物同样由 memos-api 提供读取接口（`/product/get_memory_graph`、`/product/get_event_memories`、`/product/get_memory/{id}`），控制面不持有图库地址或凭据（2026-10-23 起生效）。

## 分层与依赖方向

```
domain  ←  service  ←  transport
              ↑
           adapter（实现 service 声明的端口）
```

- `domain`：实体、值对象、typed errors，不依赖项目内任何其他包。
- `service`：用例层（对应原 Python 网关的 MemoryService 与控制台账号/审计）。需要的外部能力以**使用方定义的小接口**声明，不 import adapter。
- `adapter/*`：实现 service 的端口。`upstream`（memos-api 客户端，含图谱、事件记忆、知识库与画像模板）、`store`（控制面自有数据）、`rediscache`（可选登录节流）。
- `transport/*`：协议层，只做编解码、鉴权、错误映射，不含业务判断。`mcp`、`rest`、`dashboard`、`admin`、`auth` 五个入站面共用同一套 service；`response` 统一 MCP 与 REST 的响应字段形状；`httpx` 提供公共中间件与信封。
- `worker`：异步写入、过期清理、崩溃遗留任务回收；状态落库，停机先排空。
- `app`：**唯一**允许同时 import adapter / service / transport 的组合根。`cmd/memosd` 只依赖 `app`。
- `config`、`platform/*`：叶子包，除 `internal/domain` 的基础类型外不 import 任何内部层（depguard `layer-leaf` 放行 `domain`，禁止 service/adapter/transport/worker/app）。`platform` 下有 `version`、`logging`、`metrics`、`num`、`page`、`throttle`、`auditfile`、`zerotrust`。

这些规则由 `.golangci.yml` 的 depguard 强制，违反即 lint 失败，不靠 code review 记忆。

## 入站协议

| 入口 | 路径 | 说明 |
|------|------|------|
| MCP | `/mcp` | Streamable HTTP，5 个工具：`add_message` / `search_memory` / `delete_memory` / `add_feedback` / `get_user_profile`。工具描述即 prompt，用 golden 文件锁定 |
| REST | `/memory/*` | 给 Hermes 记忆插件与脚本用，`add` 与 MCP 一样入队即返回（`deferred=true` + `call_id`） |
| Dashboard | `/api/dashboard/*`、`/api/meta`、`/api/health`、`/api/user/*` | 管理后台与演示页，信封 `{code,data,message}`，列表键沿用 `records` / `logs` / `memListItemResponseList` / `list`；登录来自 `/__login` 会话或零信任身份 |
| Admin | `/admin/*` | 运维自动化与应急入口：租户、Token、账号、成员、邀请、死信任务；凭据是管理 Token 或平台管理员会话，不与租户 Token 混用 |
| Auth | `/__login`、`/__logout`、`__relogin`、`/__invite/{token}`、`/api/auth/*` | 控制台登录、退出、邀请落地页与账号自服务；由 `transport/auth` 挂载 |

## 租户与身份

`cube_id ≡ mem_cube_id ≡ 图库 user_name ≡ 项目 ≡ 租户`。身份只来自 Bearer token 到租户的映射，**工具参数里没有 `user_id`**。

- 任何到上游的调用都必须带租户条件；控制面不直连图库，图谱与事件记忆的租户隔离由 memos-api 的 `mem_cube_id` 参数保证。
- 控制面只经 HTTP 读图；Dream 记忆的删除倾向在上游加接口，而不是控制面直接写图。

## 上游约束

上游从 2026-10-23 起公布机器可读契约（`GET /product/contract`，当前 `contract_version=2`）。
控制面不再靠“记住怪癖”：`adapter/upstream.CheckContract` 注册在 `/readyz`，版本、时间口径、
sources 默认值、`top_k_max` 任一项不符就返回 503，不接流量。语义见上游 `docs/api-contract.md`，
控制面侧的落地约定见 `docs/module/upstream.md`。

仍然成立、需要在 `adapter/upstream` 挡住的实现细节：

- 批量的 `get_memory_by_ids` 发裸数组 `["id",...]`：契约 v2 的对象体 `{"memory_ids":[...]}` 只有实现了 v2 的上游才认，旧镜像对对象体直接 422，裸数组两边都接受。删除不校验归属，归属检查由控制面负责。
- `top_k` 按桶生效，上限 25（契约 `top_k_max`）；默认只统计 `status=activated`。
- 时间一律是真 UTC：`domain.Memory` 只有一个 `UTCTime`，控制面不做偏移换算；历史节点的“北京墙钟标 UTC”由上游在读写边界归一。
- `metadata.sources` 默认不返回；契约保证这一点，但旧膨胀数据仍在存储里，响应体积上限与 Dashboard 的流式白名单继续保留。

## 部署形态

控制面可按不同角色启动。请为部署环境配置身份验证、持久化和数据面连接。
