# Graph Memory 控制台

MemOS 记忆服务的 Web 控制台：接入说明、接口密钥、调用日志、记忆列表、记忆图谱、
模型清单、自定义抽取、用量监控。

- 页面：Vite + React 19 + react-router-dom，全部走 `/cn/*` 路由。
- 后端：独立的 Go 服务 `memosd`（另一个仓库），同源托管 `dist/`；本仓库只负责前端与接口契约。
- 本地 mock：`server/mock/`，只在开发时用，不部署。
- 接口契约：`shared/contract.ts`（前后端共用的类型）+ 仓库根目录的 `openapi.json`。

## 快速开始

```bash
pnpm install

pnpm dev:all   # mock 后端(5280) + 前端(5274)
```

打开 <http://localhost:5274/cn/apikeys>。需要 Node 20+ 与 pnpm。

要看已部署后端上的真实数据，不需要起 mock，把 `/api` 代理过去即可：

```bash
MOCK_ORIGIN=http://<后端地址> pnpm dev
```

后端是登录页鉴权时，浏览器需要先访问一次 `/__login`（代理已包含该路径）。

## 命令

```bash
pnpm dev          # 前端（:5274），/api 代理到 :5280
pnpm mock         # mock 后端（:5280）
pnpm build        # tsc + vite build，产物在 dist/
pnpm verify       # typecheck + lint + format:check + build
```

## 页面

| 页面 | 路由 | 数据来源 |
|---|---|---|
| 快速开始 | `/cn/quickstart` | `/api/meta` 的接入参数 |
| 接口密钥 | `/cn/apikeys` | 控制面的接口密钥 |
| 调用日志 | `/cn/requests` | 控制面的调用审计 |
| 记忆列表 | `/cn/memoryList` | 上游 REST；「事件记忆」走图谱接口 |
| 记忆图谱 | `/cn/memoryGraph` | 上游图谱接口 |
| 模型清单 | `/cn/models` | 上游 `/product/models` |
| 自定义抽取 | `/cn/customExtraction` | 上游按项目保存的抽取 prompt |
| 用量监控 | `/cn/usage` | 调用审计聚合 |
| 接口文档 / 关于 | `/cn/docs`、`/cn/about` | 静态文案 + `/api/meta` |

侧栏的 **项目切换** 决定所有业务接口的 `projectId`；在这个部署里
`项目 ≡ cube_id ≡ 图库 user_name ≡ 租户`。

## 分层

```
src/                  前端：页面 / 组件 / 请求封装
shared/contract.ts    前后端共用的传输类型（唯一字段定义处）
server/mock/          本地 mock（独立进程）
```

规则：

- 前端只通过 `src/lib/api.ts` 发请求，不在页面里直接 `fetch`。
- 传输形状只写在 `shared/contract.ts`，前端 `src/lib/api.ts` 再导出；后端按同一形状实现，
  参考同一份类型与 `openapi.json`。
- 列表接口一律返回 `{ records, total }`（记忆列表除外，上游契约是
  `memListItemResponseList`，`api.ts` 如实透传）。
- 统一信封 `{ code, data, message }`，`code === 0` 才取 `data`。
- 本地生成的错误走文案字典；上游返回的 message 原样展示。

## 样式

`src/styles/` 下的 `base.css`、`components.css` 是**预编译产物**，不是 Tailwind 源文件。
新增任意值工具类写进 `src/styles/console-extra.css`，否则类名在产物里不存在，会静默不生效。
详见 `AGENTS.md`。

## 环境变量

| 变量 | 作用 |
|---|---|
| `VITE_API_BASE` | 后端 origin；留空 = 同源 `/api` |
| `VITE_SERVICE_BASE` | 记忆服务对外地址（默认跟随当前访问主机的 8002 端口） |
| `MOCK_PORT` / `MOCK_DB` | mock 后端端口与落盘文件 |
| `MOCK_ORIGIN` | 前端代理目标，默认 `http://localhost:5280` |

后端的配置见它的部署清单与启动参数，不在本仓库。
