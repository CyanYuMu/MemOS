---
module: chat
packages:
  - internal/service/chat
  - internal/transport/chat
status: implemented
---

# chat

管理后台的对话页：把用户当前项目的记忆空间接成一个可多轮对话的 agent。前端只与
console-be 的 `/api/chat/*` 交互，agent runtime 的实现细节见 [rust-pi-agent](rust-pi-agent.md)。

## 职责与边界

- 负责：聊天前置校验（项目、项目 API Key、模型配置）、兼容旧会话的独立对话标识、会话受理与
  排队、会话进程的获取与生命周期、把 pi 的 JSONL 事件转成 SSE、BYOK 模型配置的读写。
- 不负责：agent 循环、上下文压缩、工具调用（pi）；MCP 鉴权与工具实现（`mcp`）；
  项目与 Key 的存储（`identity` / `tenant`）；前端页面（`../memory-console`）。
- 支持同一账号、项目下的独立对话和历史切换，项目记忆跨对话共享，模型上下文、队列和历史分开。旧固定会话完整保留，不移动或清空原目录。

## 依赖

- `internal/service/chat` 依赖 `domain` 与标准库 `log/slog`；以使用方定义的窄接口依赖
  存储、身份与运行时，不 import adapter。
- `internal/transport/chat` 依赖 `service/chat`、`httpx`、`response`，只做编解码、鉴权与
  错误映射。
- `internal/adapter/piagent` 实现本模块声明的 `Runtime` 端口；`internal/adapter/store`
  实现 `ConfigStore`；`internal/app` 负责接线。依赖方向与 `.golangci.yml` 的 depguard 一致。

## 契约

### 独立对话（2026-10-31，accepted）

首次指引需要在另一段空上下文中实际召回项目记忆。新增`chat_conversations`仅存服务端生成ID、项目、账号、标题及创建时间；不复制消息或记忆。GET `/api/chat/conversations?projectId=&offset=`返回当前账号该项目的`{records,total}`，按创建时间及ID倒序，每页20条；POST同路径接收`{projectId,title?}`并返回新对话，要求项目写权限。列表不隐式创建数据。

messages、stream的query和messages/retry/abort的body均接受可选`conversationId`。空值继续使用原固定会话；非空必须属于当前账号和项目，错误统一404，不能让调用方指定任意runtime/session路径。新会话ID参与会话标识，目录追加`conversations/<hash16(id)>/`，所有路径段都由服务端hash生成。两个新会话及旧会话的历史、队列、配置文件、锁完全隔离，MCP仍使用相同项目Key及真实session审计头。切换不会中断其他正在生成的对话，也不重发消息。

迁移0030只新增表及索引；回滚旧镜像时旧固定会话仍正常，新会话表/目录保留待恢复新版，不做破坏性反迁移。新建不启动模型、不自动提交内容；用户在指引提交介绍时才调用真实发送。此次不增加手动重命名或删除历史。模型标题扩展见下。


### 身份与权限

- 只能由控制台主体调用：`domain.PrincipalAccount`（账号会话）或零信任主体。
- 读状态、读历史、建 SSE要求 `CapProjectView`；发消息与重试要求 `CapProjectWrite`；读写模型配置要求
  `CapProjectWrite`。
- `projectId` 每请求显式传入，不从会话 cookie 推断；不可见的项目返回 403。
  例外是 `state`：页面首次进入还没选项目，项目为空或当前主体不可见时统一返回 200
  与 `hasProject=false`，由前端提示选择项目；其余接口仍走 403。

### 前置校验（产品约束）

`state` 与所有会 spawn 的接口都按同一顺序判定，缺哪一项就在响应里标出来，前端据此提示：

1. 没有 `projectId` 或项目不可见 → `hasProject=false`，提示选择项目；
2. 项目下没有可用 API Key → `hasApiKey=false`，提示创建 API Key；
3. 既没有项目级 BYOK 配置，也没有平台内置模型 → `hasConfig=false`，前端只提示
   “对话模型未配置，请联系管理员”，不提供填写表单。

平台内置模型由 `MEMOSD_CHAT_BASE_URL` / `MEMOSD_CHAT_MODEL` / `MEMOSD_CHAT_API_KEY`
提供，三项齐全时**所有项目开箱即可对话**，用户不需要先填表；项目级配置只用于覆盖。
三项都满足才允许建立 SSE 与发消息。**只有发消息会启动 pi 进程**；建流、读历史、
中断在进程不存在时都不启动新进程。API Key 的选取规则：同项目内优先当前账号创建的、
未吊销未过期的 Key，其次取最近创建的一把；`Raw` 为空的历史 Key 跳过。**任意一把
可用 Key 都可以**，因为 Key 与项目空间一一对应。

### HTTP

信封沿用 Dashboard 的 `{code,data,message}`。路径均挂在 console 角色的
`/api/chat/` 下。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/chat/state?projectId=` | 前置状态：`projectId`/`projectName`/`hasProject`/`hasApiKey`/`hasConfig`/`canChat`/`apiKey{id,label,prefix}`/`sessionId`/`contextWindow`/`autoCompact`/`piAvailable` |
| GET | `/api/chat/config?projectId=` | `{baseUrl,model,apiKeyMasked,configured,inherited}`；不回明文 Key；`inherited=true` 表示当前用的是平台内置模型 |
| PUT | `/api/chat/config` | body `{projectId,baseUrl,model,apiKey}`；`apiKey` 为空表示保留原值。前端**不提供**修改入口（模型由平台统一配置），接口只保留给运维脚本 |
| GET | `/api/chat/messages?projectId=&limit=10&before=` | `{messages,start,total,hasMore?}`；默认返回最后 10 条，`limit` 上限 50；`before` 传上一页的 `start` 取更早一页，用于向上渐进加载。页边界会向前/向后多带几条工具调用，相邻页可能重叠，前端按 `toolCallId` 与消息 id 去重。进程在时读实时结果，不在时读上一轮结束时落下的历史快照；**不会因此启动进程** |
| GET | `/api/chat/stream?projectId=` | SSE；建连前完成前置校验，不满足直接返回 JSON 错误而不是 SSE。**只订阅、不启动进程** |
| POST | `/api/chat/messages` | body `{projectId,text}`；受理即返回 202 与 `{id,queued}`，不等待 pi 的 prompt 应答。运行中再发进入队列，由会话串行派发 |
| POST | `/api/chat/retry` | body `{projectId}`；把最早一条待重试消息重新入队并回 202 `{id,queued}`；没有待重试消息时 404。前端「重试」走这里，不自己猜最后一条用户消息 |
| POST | `/api/chat/abort` | body `{projectId}`；中断当前轮并清空排队。进程不存在时直接成功，不再拉起进程 |

`state` 在 `projectId` 为空时返回 200 与 `hasProject=false`，不报错。

### 受理与排队

- 消息进入会话队列前先追加到磁盘（`runtime/pending.jsonl`，逐条 fsync），再返回 202：
  **被 202 受理的消息不会静默丢失**。
- 每个会话一次只发一个 prompt；`agent_settled` 后出队并发送下一条。运行中再次发送
  进入队列，SSE 用 `message_accepted{queued:true}` 与 `chat_status{state:"queued"}` 告知前端。
- `abort` 中断当前轮并清空队列，队列项以 `cancelled` 落盘，前端收到对应状态。
- 控制面重启后不自动重发（避免同一轮重复执行）：遗留队列项转为 `needs_retry`，通过
  `chat_status` 与 `chat_error{retryable:true}` 告诉用户重试。
- prompt 应答超时为 3 分钟：自动压缩是同步的一次 LLM 调用，不能用 10 秒判定失败。
  超时或写入失败时进程状态不可知，adapter 会先停掉进程，再把这一条放进 `needs_retry`。
- 单个会话的待发队列上限 20 条、待重试上限 20 条：超出直接 429（`chat_busy`），
  不让一条在跑的会话无条件收下所有请求。
- `needs_retry` 是一个可离开的状态：`POST /api/chat/retry` 每次取走最早一条重投，
  跑完后计数归零、状态回到 `idle`；新消息不会清空待重试列表。

### SSE

2026-10-31（implemented）：Pi执行事件的SSE信封增加可选`submissionId`，由Session在广播时、持锁读取实际执行中的head.ID，不采用最后受理或队尾消息。它对应message_accepted及202返回的id；排队消息不能抢走前一轮的思考、工具和回答。控制事件不附带此字段。历史由Pi实际执行顺序的transcript解析，实时事件缺少可验证归属时不猜测。此字段只做关联，不改变鉴权、队列或模型输入，旧客户端可忽略。

- `Content-Type: text/event-stream`，每条事件一帧：`data: {"seq":N,"type":"...","data":{...}}`。
- `type` 直接透传 pi 的事件名（snake_case）：`agent_start` / `agent_end` /
  `agent_settled` / `turn_start` / `turn_end` / `message_start` / `message_end` /
  `message_update` / `tool_execution_start` / `tool_execution_update` /
  `tool_execution_end` / `compaction_start` / `compaction_end`。
  `message_update` 里只带 `text_*` 与 `thinking_*` 增量，toolcall 参数流不下发。
- 控制面追加：
  - `chat_ready`：`{sessionId, contextWindow, state, running, compacting, queued, needsRetry}`，
    订阅时立即下发；`state` 与 `chat_status` 同义，断线重连后靠它恢复「启动/压缩」；
  - `chat_status` 的 `needsRetry` 是待重试条数；`state` 不会因为有待重试就变成
    `needs_retry`（那样会盖住真实的空闲/运行状态），前端用计数单独展示提示；
  - `chat_status`：`{state, queued, running, compacting, message?}`，`state` 取
    `starting`（进程在起或恢复，还没有 `agent_start`）/ `queued` / `running` /
    `compacting` / `idle` / `needs_retry`；`queued` 只数**等待中**的消息，队头不算；
  - `message_accepted`：`{id, text, queued}`，与 202 响应一一对应；
  - `chat_error`：`{message, code, retryable}`，前端按 `retryable` 决定是否给重试入口。
- 心跳是每 15s 一行 `: ping` **注释**，不是事件类型，前端不会收到 `ping` 事件。
- `seq` 由会话 broker 单调分配，**进程重启不重置**；前端目前按 `type` 渲染，`seq` 只用于
  调试与多标签页去重。
- 同一会话允许多个 SSE 订阅者（多标签页），事件广播；单个订阅者写阻塞时丢弃该订阅者
  而不是阻塞 pi 的 stdout。被摘除的订阅者由前端自动重连，重连后用 `chat_ready` 与历史
  快照对账。

### 会话与目录

- 旧会话的 `sessionId = "chat-" + hex(sha256(accountID + "|" + projectID))[:24]`，稳定、不可枚举。
  平台管理员 Token 没有账号 ID，统一用 `admin-token`，避免所有空账号共用同一个空目录。
- 目录：`<MEMOSD_CHAT_DIR>/<sha256(projectID)[:16]>/<sha256(accountID)[:16]>/`，
  下分 `agent/`（`PI_CODING_AGENT_DIR`）、`sessions/`（`--session-dir`）、
  `cwd/`（进程工作目录，保持为空）与 `runtime/`（本服务自己的文件：会话锁
  `<sessionId>.lock`、历史快照 `history.json`、受理队列 `pending.jsonl`）；
  新独立对话在上述账号目录下追加`conversations/<hash16(conversationId)>/`，各子目录结构相同；任何路径都由服务端生成，不拼接用户输入。
- 同一会话任一时刻只有一个 pi 进程：spawn 前对 `runtime/<sessionId>.lock` 取排他
  `flock`，进程确认退出后才释放，再允许下一次 spawn。孤儿进程在启动时按 cmdline 回收。
- 上下文窗口取 `MEMOSD_CHAT_CONTEXT_WINDOW`（默认 131072），`maxTokens` 取
  `MEMOSD_CHAT_MAX_TOKENS`（默认 16384）；启动后显式发
  `set_auto_compaction {enabled:true}`。`compaction_start` / `compaction_end` 透传给
  前端，页面显示“正在压缩上下文”，不阻塞输入。

### 持久化与审计

**数据源（都在 `<MEMOSD_CHAT_DIR>` 下，dev `/data/chat`）**：

| 文件 | 谁写 | 用途 |
|---|---|---|
| `<proj>/<acct>/sessions/*.jsonl` | pi 自己 | 原始 transcript，`--resume` 的唯一来源，也是会话内容的最终真值 |
| `<proj>/<acct>/runtime/history.json` | 本服务 | 每轮 `agent_settled` 后的消息快照；进程不在时 `messages` 读它 |
| `<proj>/<acct>/runtime/pending.jsonl` | 本服务 | 受理队列与重试（accepted/done/cancelled/failed/retry） |

- 目录根是 `MEMOSD_CHAT_DIR`，落在 PVC 上（dev 5Gi、prod 10Gi hostPath，单节点）。Pod 重启、
  进程被杀都不丢；节点级故障或换节点才会丢，因为 hostPath 绑在 `127.0.0.1`。
- 我们的写路径都是「临时文件 + fsync + rename + fsync 目录」；`pending.jsonl` 逐条 fsync
  后才回 202。pi 的 JSONL 由 pi 自己落盘，本服务只读不写。
- **不自动清理 transcript**：会话目录按「项目/账号」隔离并长期保留（可能含用户原始对话）。
  磁盘剩余低于 `MEMOSD_CHAT_MIN_FREE_DISK` 时拒绝新对话并记 warn，启动时也会打印
  `chat storage ready` 与剩余空间。这里看的是**节点磁盘**剩余：hostPath/local PV 的
  `storage: 5Gi/10Gi` 只是声明值，kubelet 不按它做配额，卷填满的表现是节点盘紧张。
- 已知限制：10Gi 需要监控（SQLite 与 transcript 共用 data 卷）；没有按天数的清理策略，
  需要归档时由运维按目录处理。

**审计**：`chat.send` / `chat.retry` / `chat.abort` 各记一条，字段是项目、会话 ID、
主体（`principal=admin_token` 会明确标出管理员 Token 调用）、消息 ID/正文长度/是否排队、
HTTP 状态与错误安全文案。**消息正文不进审计**，留在 transcript 里。

## 单副本假设

会话对象、受理队列、订阅者、`MaxProcs` 计数都在进程内存里，会话锁是同一内核内的
`flock`，会话文件在 Pod 本地盘。因此本模块的前提是**单副本**：多副本需要会话粘滞
路由（同一会话的 SSE 与 POST 落到同一 Pod），跨 Pod 的配置变更/Key 吊销也要靠 TTL
到期收敛。`MEMOSD_CHAT_MAX_PROCS` 是**每进程**上限，不是全局上限。

## 配置项（`internal/config`）

| 键 | 默认 | 说明 |
|----|------|------|
| `MEMOSD_CHAT_DIR` | 空 | 聊天会话根目录；空表示本进程不挂载 `/api/chat/*` |
| `MEMOSD_CHAT_PI_BIN` | `pi` | pi 可执行文件路径 |
| `MEMOSD_CHAT_MAX_PROCS` | `12` | 全局在途 agent 进程上限；满时先驱逐空闲会话，全部在生成才 429 |
| `MEMOSD_CHAT_IDLE_TTL` | `15m` | 会话进程空闲回收时间 |
| `MEMOSD_CHAT_MCP_URL` | `http://127.0.0.1:8002/mcp` | pi 连接的同进程 MCP 地址 |
| `MEMOSD_CHAT_CONTEXT_WINDOW` | `131072` | 写入 models.json 的上下文窗口 |
| `MEMOSD_CHAT_MAX_TOKENS` | `16384` | 写入 models.json 的单次输出上限 |
| `MEMOSD_CHAT_PROC_MEMORY` | `134217728` | 准入前要求的内存余量（字节）；不足直接 429 |
| `MEMOSD_CHAT_MIN_FREE_DISK` | `268435456` | 会话目录所在盘的准入阈值（字节）；不足直接 503，避免写坏 transcript |
| `MEMOSD_CHAT_SYSTEM_PROMPT` | 空 | 整段替换 pi 的系统提示；空时用内置的“记忆空间助手”提示 |
| `MEMOSD_CHAT_TOOLS` | `search_memory,add_message,get_user_profile` | 直接暴露给模型的 MCP 工具，逗号分隔。破坏性的 `delete_memory` / `add_feedback` **默认关闭**，确需删除能力时显式列出 |
| `MEMOSD_CHAT_BASE_URL` | 空 | 内置对话模型的上游网关；与另外两项同时配置才生效 |
| `MEMOSD_CHAT_MODEL` | 空 | 内置对话模型名 |
| `MEMOSD_CHAT_API_KEY` | 空 | 内置模型的网关密钥，只来自进程环境，不落库、不回显 |

## 错误与边界情况

- 缺项目 / 缺 Key / 缺配置：`state` 正常返回标记；`stream` 与发消息返回 409
  （`httpx.CodeConflict`，message 说明缺哪一项）。前端按 `state` 提前拦截，正常不会走到这里。
- 进程数达到 `MaxProcs`：发消息前先同步预检——还有空位、或能驱逐一个最久空闲的会话，
  就收下并回 202；否则直接 429（`chat_busy`）。预检通过后真正的准入仍可能因为并发
  抢占而失败，这种情况已经在 202 之后，走 SSE `chat_error{code:"chat_busy",retryable:true}`
  并把消息留在 `needs_retry`。准入等待与驱逐都在有界时间内完成。
- 内存余量不足一个进程预算（`MEMOSD_CHAT_PROC_MEMORY`，默认 128MiB）：同步返回 429，
  不等内核 OOM。
- pi 二进制不存在或无法执行：`state.piAvailable=false`，前端直接显示不可用；若绕过前端
  发消息，会在 202 之后收到 `chat_error{code:"chat_unavailable",retryable:true}` 并进
  `needs_retry`。
- pi 进程崩溃：当前轮中断，向前端发 `chat_error{retryable:true}`；下一条消息重新 spawn，
  会话文件保留，继续同一 `sessionId`。排队的后续消息不受影响。
- 控制面重启：所有进程消失，SSE 断开；遗留 `pending.jsonl` 不自动重发，转为
  `needs_retry` 并通过 `chat_status` / `chat_error` 告知用户，历史由快照补齐。
- 模型配置变更：停止该项目下所有会话进程，下一次发消息按新配置重建；不迁移历史上下文。
  换配置与派发可能交错，因此有两条兜底：重启期间派发暂停（计数式 `quiescing`，Stop/
  StopTenant/换配置重叠时先结束的那次不会提前放行），以及每个进程带着启动时的配置代次
  注册——代次已被替换的进程不进入 `agents`，直接停掉，由准入按新配置重来。取消请求 ctx
  时等不到在途 spawn 也不影响正确性（`refreshSpec` 的等待是 best-effort）。
- 一轮只归一个进程收尾：`turnAgent` 记录当前轮派给了哪个进程，`agent_settled` 与进程退出
  只有执行者的才收尾。被停掉的旧进程会推兜底 `agent_settled`，此时可能已经在跑下一条，
  不能把新一轮记成完成。
- 正常停止/重启时先 `abort`：成功则给 pi 300ms 把中断的这轮落到 JSONL，再 kill 进程组；
  abort 超时或失败直接 kill。驱逐走快速路径：不发 abort，直接 kill 进程组，等进程退出
  并释放会话锁后才允许重拉。
- 单轮最长时长看门狗：空闲 10 分钟、单轮总时长 20 分钟仍未 `agent_settled`，判定为卡死
  （硬上限按轮次开始时间算，事件不断的长轮次也会被收掉）；
  先对账（`get_state` 显示不在生成就按正常结束收尾，且只收尾当时那条），仍显示在生成
  才杀进程，并把这一轮放进 `needs_retry` 后推可重试错误。
- 对外文案只写“发生了什么、你能做什么”，不透出 pi 路径、命令、上游地址或 Key。

## 测试与验证

- `service/chat`：前置校验顺序（无项目 / 无 Key / 无配置 / 全满足）、Key 选取回退、
  sessionId 稳定性、配置保存后停用项目内会话、受理回执与排队计数。
- `transport/chat`：`state` 空项目、SSE 帧格式、前置不满足时的状态码、断连注销。
- `adapter/piagent`：用 Go 测试辅助进程模拟 pi 的 JSONL（`GO_WANT_FAKE_PI`），覆盖
  spawn 与配置文件生成、prompt 事件、abort、进程退出、MaxProcs；并发 `Session` 只起
  一个进程；满槽驱逐而非 429、全部在生成才 429；`Messages`/`Abort` 无进程可用；
  历史快照原子替换；订阅者溢出后重连补齐；漏 `agent_settled` 由看门狗恢复；
  `flock` 保证同一会话单写者；受理后进程重启不静默丢消息；spawn 在途时换 Key（用已取消的
  ctx 触发）之后到来的消息只能由新凭据的进程执行；旧进程的事件与退出不会收掉下一轮。
- 集群验证：dev 部署后用 superduck 走「发送→受理→生成中再发→排队→自动续发→
  压缩提示→断线重连→驱逐后恢复→容量满提示」，记录 URL、动作、观察与截图。

## 参考来源

  命令、事件、spawn 参数与容量约束。
  响应字段；`modes/json-event.rs`：事件投影；`core/model-config.rs`：models.json 形状。
  请求层写法。

## 实现记录

See the public version-control history.
## 个人助手与回答反馈（2026-10-31，implemented）

默认人格改为版本化soul.md：自然友好、按用户偏好调节详略，引用真实记忆，受理不冒充提取完成；默认不暴露工具/存储等实现名词。参考问题由同一轮回答尾部的结构化块给出，前端独立展示，不增加模型调用。

回答评分独立于记忆纠错：GET/PUT `/api/chat/feedback`，显式projectId/responseId（64位小写SHA256），vote为-1/0/1；账号来自登录身份，项目按CapProjectView严格授权。反馈按账号、项目、回答标识隔离保存，0取消，只保存评分与更新时间，不保存正文，也不调用记忆反馈工具。新增SQLite表不改既有凭据和聊天记录。

### 写工具与项目权限（2026-10-31，accepted）
聊天默认开放真实删改后，POST messages/retry要求项目写权限；只读成员的state返回readOnly=true、canChat=false，历史、状态、评分仍可读。必须在生成前拒绝，不能让模型持有项目Key替只读成员越权修改。


2026-10-31（独立对话源码批次）：新增0030入口元数据、账号/项目范围内分页与创建；前端使用独立URL选择，旧固定历史保留。运行时配置、锁、队列和transcript按conversation隔离；真实session经MCP关联头进入上游、审计和业务事件。后端相关race/shuffle及全量vet/test/tidy/docs/build通过，增量lint无新问题；全量check仍8项既有lint。首次全量复现原关闭后派发写盘缺口，新增启动中关闭用例先稳定失败，最终握手取消、未注册进程直接回收和后台等待修复后两项关闭/重试用例各3次通过，原重启用例曾连续10次通过。两位独立只读评审复审关闭P2；仍待完整首次指引与dev真实链路验收，未将此源码批次称为交付。

2026-10-31（指引与轮次归属源码批次）：SSE可选submissionId与真实head绑定，覆盖A执行/B排队的负向用例；控制事件保持兼容。两位独立只读复审通过。make fmt-check lint-new vet test tidy-check docs-check build通过，全量lint保留原2funlen/6modernize；同时修复上一批清理context的2项新增contextcheck。前端完整六步指引、按执行归属的不可变轮次快照通过41项测试及verify；最终dev真实浏览器与模型链路仍待验。

## 2026-11-01 标题与反馈补齐（implemented）

- 反馈现有SQLite评分表增加意见、对话ID、回答摘录；GET/PUT同一路径扩展可选字段，旧客户端保持兼容。文字独立保存成功后才可发送纠正消息；发送失败仍保留已保存意见。取消评分删除该条反馈。项目、账号及对话归属均校验，不启动模型、不将普通评分自动写为记忆。
- 首轮最终回答在同一次生成末尾增加JSON字符串元数据 `<graph-memory-title>"简短主题"</graph-memory-title>`；无额外模型请求。前端过滤元数据并刷新历史标题；参考问题可在标题前后出现。无有效标题保留未命名，不用截取用户输入冒充模型生成。
- Runtime在正常历史快照落盘后读取最早有效最终assistant标题，通过窄存储端口仅更新当前项目/账号/对话且标题为空的行；不依赖浏览器连接。已有显式标题不覆盖，旧未命名对话在下一次真实回复时可补全。后台不扫描旧会话或自动发消息。
- SQLite只保存对话元数据；Pi仍唯一保存消息与上下文，session ID和路径不随标题变化。增量迁移仅加反馈列，旧镜像忽略新列即可回滚；新标题不要求修改Pi分发文件或数据面。

## 2026-11-02 可恢复的对话归档（implemented）

验收清理使用`PUT /api/chat/conversations/{id}/archive`，body包含projectId和archived布尔值。仅当前项目/账号可修改，归档不启动模型、不删除Pi文件或记忆、不改变会话ID；archived=false可恢复。归档入口从默认历史和新请求的会话选择中隐藏，既有生成不被归档动作中断，因此验收在agent_settled后归档。SQLite0034追加archived列，默认0兼容旧记录；回滚旧镜像会再次显示归档入口但不会损坏正文。验收仅归档已登记的临时对话。

实现验证：SQLite关闭重开保持归档并可恢复；跨项目/账号修改拒绝；归档后不能发起新轮次；HTTP布尔参数/权限/恢复及不触发模型测试通过，完整make check通过。
