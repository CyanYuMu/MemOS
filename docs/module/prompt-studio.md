---
module: prompt-studio
packages:
  - internal/service/prompt
  - internal/transport/dashboard
  - internal/adapter/upstream
status: implemented
---

# prompt-studio

提示词工作台的控制面部分：把数据面（dumemos 注册表与版本表）的项目级提示词能力原样代理到控制台，控制面不保存任何默认文本、正文或版本。

status 含义：`draft` 还在写，不得写代码；`accepted` 契约已定，可以开始实现；`implemented` 代码与测试已落地并与本文一致。

## 职责与边界

- 负责：`/api/dashboard/prompts/*` 八条路由的入参解析、项目级授权、把请求代理给上游、把上游响应转成控制台的 camelCase 形状、错误映射、限流与请求上限、保存/还原的元数据审计。
- 负责：`author` 由登录账号填入，忽略客户端传值。
- 不负责：提示词注册表、默认模板文本、版本存储、校验规则与试跑（都在数据面）。控制面没有默认文本，也不缓存上游响应。
- 不负责：前端页面、diff 算法与历史抽屉（`memory-console`）。
- 不负责：旧的 `prompt-config/*` 五条路由的语义改造。它们保持可用，只是内部改由数据面读写 `extract.chat`；本文只把它们标记为 deprecated。

## 依赖

- `internal/service/prompt`：用例层，声明 `Gateway` 端口（使用方定义），持有限流与请求上限。`transport` 只调用它。
- `internal/adapter/upstream`：实现 `prompt.Service.Gateway`，走数据面 `POST /product/prompts/*` 八条接口。
- `internal/transport/dashboard`：路由、鉴权规则、字段转换与错误映射，复用 `transport/auth`、`transport/httpx`。
- 被 `internal/app` 组合：`deps.PromptStudio` 为 nil 时八条路由返回 503（与现有 `Prompts` 端口一致），不伪造空数据。

依赖方向遵守 `domain ← service ← transport`；`domain` 里只放实体与错误类型，不 import 项目内其他包。

## 契约

### 路由与权限

全部 `POST`，前缀 `/api/dashboard/prompts/`，信封 `{code,data,message}`，`code === 0` 才解包 `data`。

| 路由 | 入参（除 projectId） | 上游 | 权限 |
|------|----------------------|------|------|
| `prompts/list` | — | `POST /product/prompts/list` | viewer（`auth.InBody(CapProjectView)`） |
| `prompts/detail` | `key` | `POST /product/prompts/detail` | viewer |
| `prompts/versions` | `key`、`lang`、`limit`（缺省 20，收敛到 1..100）、`beforeVersionId` | `POST /product/prompts/versions` | viewer |
| `prompts/validate` | `key`、`lang`、`body` | `POST /product/prompts/validate` | editor（`auth.InBodyStrict(CapProjectWrite)`） |
| `prompts/save` | `key`、`lang`、`body` **或** `restoreFromVersionId`（二选一）、`note`、`baseVersionId` | `POST /product/prompts/save` | editor（`InBodyStrict`） |
| `prompts/reset` | `key?`、`lang?` | `POST /product/prompts/reset` | editor（`InBodyStrict`） |
| `prompts/test` | `key`、`lang`、`body?`、`input`、`compareDefault` | `POST /product/prompts/test` | editor（`InBodyStrict`） |
| `prompts/generate` | `key`、`lang`、`instruction`（≤2000 字）、`baseBody`（≤20000 字） | `POST /product/prompts/generate` | editor（`InBodyStrict`） |

- `projectId` 是**八条路由都必须带**的入参（包括 `prompts/validate`：控制面靠它判定权限）。它经 `transport/auth` 判权后由框架写回请求体，处理器不读客户端自报的值；`InBodyStrict` 要求显式给出，缺省返回 400。发往上游的请求只带上游声明需要的字段，不转发多余字段（例如 `validate` 只发 `key`/`lang`/`body`）。
- `author` 取 `domain.Principal.Username`（管理 Token 主体为 `admin-token`），请求里带的 `author` 被忽略。
- 旧的 `prompt-config/*` 五条继续注册；`api/openapi.dashboard.yaml` 里标记 `deprecated: true`，行为与文档形状不变。

### 请求字段

请求用 camelCase：`projectId`、`key`、`lang`、`body`、`note`、`baseVersionId`、`restoreFromVersionId`、`beforeVersionId`、`limit`、`input`、`compareDefault`、`instruction`、`baseBody`。空串按未传处理（与控制台前端一致的既有约定）。

### 响应字段转换

唯一规则：信封里的键从 snake_case 转 camelCase（`version_id` → `versionId`、`version_no` → `versionNo`、`has_more` → `hasMore`、`locked_suffix` → `lockedSuffix`、`parse_ok` → `parseOk`、`fallback_triggered` → `fallbackTriggered`、`raw_output` → `rawOutput`、`latency_ms` → `latencyMs`、`parent_version_id` → `parentVersionId`、`used_slots[].version_id` → `usedSlots[].versionId`、`output_contract.required_keys` → `requiredKeys` 等）。

版本号有两层：`versionId` 是库里的全局自增编号，`versionNo` 是该（项目、key、语言）槽位从 1 开始的序号，界面显示后者。`state`、`effective`、`versions` 每项、`save` 结果（顶层与 `effective` 各一份）、`reset` 清单项都带 `versionNo`；409 的 `data` 同时给 `currentVersionId` 与 `currentVersionNo`。

默认槽位没有版本：`state` 与 `effective` 里的 `updatedAt`、`author` 原样是 `null`（不是空串）。`sampleInputs[]` 带 `lang`；`runs[]` 带 `usedSlots`（`[{key,lang,source,versionId}]`，`source` 取 `draft`/`project`/`default`）与 `warnings`（`[{code,message}]`）；`usage` 是开放映射，上游多给的 `calls` 等键一并透传（键做 snake→camel）。

以下内容**原样透传，不转换键名**：

- 注册项的 `inputSchema`；
- `sampleInputs[].input`；
- `runs[].output`（例如 `memory list`、`memory_type` 保持 snake_case）；
- `defaults` 与 `effective` 里以语言代码（`zh`、`en`）为键的那一层。`title`/`summary`/`placeholders[].description`/`outputContract.description` 的语言映射同理（键本来就是语言代码）。

`runs[].usage` 的键按 camelCase 转换（示例：`prompt_tokens` → `promptTokens`）。

`runs[].rawOutput` 之外没有正文裁剪：`output` 是上游按 key 解析后的结构，字段原样透传。

### 数据面与版本

- 数据面按项目（`mem_cube_id`）隔离；某个槽位是（项目、key、语言），当前生效版本是版本表里该槽位最大的 `version_id`，`kind=reset` 或没有行表示内置默认。
- 控制面不落库、不缓存，每个请求都直连数据面一次；多副本或重启不会读到过期值。
- `prompts/save` 的 `body` 与 `restoreFromVersionId` 互斥：两个都给或都不给返回 400，文案说明只填一个。
- `prompts/save` 的 `baseVersionId` 用于乐观并发：与当前生效版本不一致时数据面返回 409，控制面原样返回 409 并把当前版本号放进 `data.currentVersionId`。
- `prompts/save` 的 `restoreFromVersionId` 与 `body` 二选一；回滚就是以旧版本内容追加一条 `kind=rollback`。

### 请求上限与限流

控制面侧（数据面还会再检查一次，因为它可被其他客户端直接调用）。请求体上限在 `transport/dashboard` 挡（它才有原始字节），其余在 `service/prompt` 挡：

| 项 | 上限 | 检查位置 |
|----|------|----------|
| `prompts/test`、`prompts/validate` 请求体 | 256 KiB | transport（重新编码解码后的 body 度量） |
| 草稿正文（`test`/`save` 的 `body`） | 20000 字符 | service |
| 草稿正文（`validate` 的 `body`） | 不限制长度 | `validate` 必须始终 200，超长由数据面用 `errors[].code=too_long` 返回 |
| 对话类输入 | 30 条消息、合计 12000 字符（按 `input.messages` 计） | service |
| `note` | 200 字符 | service |
| `instruction` / `baseBody`（`generate`） | 2000 / 20000 字符 | service |

逐条限制对边界值放行（20000 字、30 条都合法），超限返回 413、信封码 `41300`（`note` 例外：它与数据面的保存校验同口径，返回 400）。`validate` 只在请求体整体超过 256 KiB 时才 413，正文超长永远走 200 + `too_long`。

限流是进程内令牌桶，键为 `projectId`，同时对登录账号再压一层，任一超限即拒绝：

| 路由 | 突发 | 补充速率 | 同时在途 |
|------|------|----------|----------|
| `prompts/test` | 3 | 6 次/分钟 | 每项目 1 个（与改写互不占用） |
| `prompts/generate` | 2 | 4 次/分钟 | 每项目 1 个（与试跑互不占用） |

- 项目桶与账号桶**先检查再扣减**：两个桶都够才各扣，任一不足时一个令牌都不消耗，返回的 `retryAfterSec` 按补足所需令牌的时间算。
- `compareDefault=true` 一次请求扣 **2 个令牌**（数据面实际调用两次模型），在途仍算 1 个；单次请求要的令牌数超过桶容量时按容量算，避免突发 1 的配置下对比永远跑不了。
- 没有账号身份的调用（管理令牌，`Principal.Actor()` 为空时）用固定账号键 `admin-token`，不跳过账号维度。

数值由 `internal/config` 从环境变量读取：`MEMOSD_PROMPT_TEST_BURST`、`MEMOSD_PROMPT_TEST_REFILL_PER_MIN`、`MEMOSD_PROMPT_GENERATE_BURST`、`MEMOSD_PROMPT_GENERATE_REFILL_PER_MIN`，默认值即上表。


### 审计

只有 `prompts/save` 与 `prompts/reset` 写审计（`Tool` 为 `dashboard.prompts.save` / `dashboard.prompts.reset`），读接口与 `validate`/`test`/`generate` 不写：

- 输入只记元数据：`promptKey`、`lang`、`baseVersionId`/`restoreFromVersionId`、`noteLength`、`promptSha256`、`promptLength`；不带正文、不带 `note` 原文、不带试跑输入与输出。
- 试跑与 AI 改写的输入输出不进审计、不进日志。日志只记路由、项目、耗时与结果分类。
- 失败调用照常记一条审计，`errMessage` 用与响应相同的安全文案。

### 对外文案

面向用户的 `message` 只写“发生了什么”和“你能做什么”：不出现上游地址、上游状态码、字段名、版本号判定依据、限流内部参数。`openapi.dashboard.yaml` 的 `description` 面向开发，可以用技术语言。

## 错误与边界情况

| 上游（HTTP + `code`） | 控制面类型 | 状态码 | 信封 `code` | `data` |
|---|---|---|---|---|
| 400 `invalid_prompt` / `invalid_input` | `domain.ErrInvalidInput`（带逐条问题时是 `InvalidPromptError`） | 400 | 40000 | 有逐条问题时是 `{errors:[{code,message}]}`，否则 null |
| 404 `unknown_prompt_key` | `domain.ErrNotFound` | 404 | 40001 | null |
| 409 `version_conflict` | `domain.VersionConflictError` | 409 | 40900 | `{currentVersionId}` |
| 413 `input_too_large` | `domain.ErrTooLarge` | 413 | 41300 | null |
| 429 `busy` | `domain.BusyError` | 429 | 42900 | `{retryAfterSec}` |
| 502 `llm_error` | `domain.ErrModelFailed` | 502 | 50200 | null |
| 504 `llm_timeout` | `domain.ErrModelTimeout` | 504 | 50400 | null |
| 其他 4xx/5xx、超时、连不上 | 既有映射（`ErrUpstreamDenied` / `ErrUpstreamUnavailable`） | 502/503 | 50143/50300 | null |

- 429 同时设 `Retry-After` 头（秒，至少 1）。控制面自己的令牌桶和上游 `busy` 走同一条路径；`retryAfterSec` 就是 `Retry-After` 的值。
- `prompts/test` 单次 LLM 调用失败（`llm_error`/`llm_timeout`）不会让整个请求失败：上游返回 200，`runs` 里该次 `ok=false` 并带 `error.code`/`error.message`；控制面按同样形状透传，`compareDefault` 时另一边不受影响。
- 保存内容与当前生效内容完全相同时不新增版本，返回当前版本，控制面照常返回 200。
- 409 的文案固定为“提示词已被他人更新，请刷新后再保存”，`currentVersionId` 与 `currentVersionNo` 供页面展示对照；不暴露“谁改的”。数据面的错误体把它放在 `data.current.version_id`（联调确认），适配层同时接受扁平的 `current_version_id`。
- 上游 404 分两种：带 `code=unknown_prompt_key` 的是“这条提示词不存在”；其余（如路由未部署的 `{"detail":"Not Found"}`）按控制面侧的接口问题报 502，不让用户以为自己选错了提示词。
- 上游返回的 `message` 在 `invalid_prompt`/`invalid_input` 等 400 分支可以透传（数据面保证面向用户）；为空时用本模块的兜底文案，不把空串丢给页面。
- 数据面的 400 带 `data.errors` 时，控制面把这些逐条问题原样放进响应 `data.errors`：只有“请按错误列表修改”却没有列表，页面无法告诉用户改哪里。
- 控制面自己拒绝的请求（参数类型、上限、限流）不会打到上游。
- `prompts/reset` 省略 `lang` 表示该 key 全部语言，省略 `key` 表示整个项目；只对当前不是默认的槽位追加 `kind=reset` 行，返回 `{"reset": [{key, lang, versionId}]}`。没有可还原槽位时 `reset` 是空数组，不是 404。
- `prompts/save` 的 `effective` 是该槽位的扁平对象（`body`/`source`/`versionId`/`updatedAt`/`author`），不再按语言分层；`prompts/detail` 的 `effective` 才是以语言为键的映射。
- 注册表的 `syntax` 原样透传，当前取值有 `dollar`、`brace`、`format`；具体的 key 集合由数据面注册表决定，控制面不做枚举。

## 测试与验证

- `internal/adapter/upstream`：`httptest` 假上游覆盖八条接口的成功解码与错误码映射（400/404/409/413/429/502/504、连不上、响应不可解析），断言请求 payload 是 snake_case 且带 `mem_cube_id`。
- `internal/service/prompt`：上限（正文/note/消息条数/总字符/请求体）、令牌桶的突发与补充、在途上限、账号维度与项目维度任一超限都拒绝，以及并发释放。
- `internal/transport/dashboard`：表驱动 + `t.Parallel()`，覆盖每条路由的成功形状、字段转换（camelCase 与透传部分）、viewer 对写路由 403、审计只记元数据（断言审计里没有正文）。
- 契约：`test/golden/dashboard/routes.json` 钉住八条路由与旧五条的形状（含 `versionNo`、`usedSlots`、`warnings`、默认槽位的 `null`，以及 400/404/409/413/429 的错误形状）；`TestGoldenErrorNamesMatchStatus` 强制“条目名里的状态码等于记录的 `status`”，防止再出现名字写 413、实际记 200 的空锁；`TestOpenAPIDashboardContract` 双向对齐路由表与 `api/openapi.dashboard.yaml`，八条路由的错误响应都引用统一的 `ErrorEnvelope`。
- 审计与日志：`TestPromptSaveAuditHasNoBody`、`TestPromptTestAndGenerateLeaveNoTrace`、`TestPromptAuditInputKeyWhitelist` 断言 save/reset 的审计只有元数据、test/generate 完全不写审计，且捕获到的日志里没有正文/输入/指令的标记串。
- 端到端：`scripts/e2e-prompts-local.sh` 用临时目录起 `memosd`（console 角色），指向 `MEMOS_API_BASE_URL`（默认 `http://127.0.0.1:18101`，即 dumemos 的 `scripts/dev/prompt_service.py`），播种 editor/viewer 账号与项目，打印登录方式与调用示例；状态只落在临时目录，Ctrl-C 或退出即删。`PROMPT_E2E_EXIT_AFTER_SMOKE=1` 时跑完冒烟就退出。上游没起也能启动（接口返回 503），不会阻塞前端联调。
- 前端契约：`make openapi-json OUT=<path>` 从 `api/openapi.dashboard.yaml` 派生 JSON（`scripts/_openapijson`，用已固定的 kin-openapi 加载并校验），生成的文件放在任务目录 `contract/openapi.json`，不写前端仓。JSON 是派生物，不手写。
  - `make check` -> 退出码 0（fmt-check/lint/vet/test/tidy-check/docs-check/build 全绿）。
  - `go test -race -shuffle=on ./internal/...` 覆盖本车道全部新增测试。
  - 与 infra 的真实数据面（`scripts/dev/prompt_service.py`，19 条注册项）联调：list 19 条、detail 的默认值与透传字段、save（作者取自登录）、409 + `currentVersionId`、versions、reset 的 `{"reset": [...]}`、旧 `prompt-config/detail` 兼容、viewer 403、20001 字草稿 413 全部符合预期；`prompts/test` 在数据面尚未实现时返回 502（路由缺失），实现后需再跑一次。
  - `scripts/e2e-prompts-local.sh` 在 18101 假上游上跑通 editor/viewer 登录、list、detail、save（作者取自登录、忽略冒名）、过期 `baseVersionId` 得到 409 + `currentVersionId`、versions、test（usage 已 camelCase）、generate、reset（`{"reset": [...]}`）、viewer 写路由 403；退出后临时目录与两个端口都释放。
- 验收场景里属于本车道：S1（列表/详情形状）、S3（项目隔离的代理与 409）、S7（校验错误的映射）、S9（限流与 429 形状）、S10（viewer 403）、S11（旧路由可用）、S12（审计与日志无正文）。

## 参考来源

- `docs/module/dashboard-routes.md`（信封、错误码、审计口径、既有 prompt-config 五条）。
- `docs/module/upstream.md`（上游客户端、时间与体积口径、错误映射）。
- `api/openapi.dashboard.yaml`（控制台契约唯一真源，`prompts/*` 在此声明）。

## 实现记录

See the public version-control history.
