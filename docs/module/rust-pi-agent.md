---
module: rust-pi-agent
packages:
  - internal/adapter/piagent
status: implemented
---

# rust-pi-agent

把 rust-pi（Pi v1.3.0 的 Rust 复刻，静态 musl 二进制）作为 chat 页面的 agent runtime：
console-be 以子进程方式驱动它，前端经 console-be 转发对话流。本文由 rust-pi 侧集成
owner 起草，供 Chat 接入评审；status 到 accepted 才动代码。

## 职责与边界

**pi 负责**：agent 循环（多轮工具调用、流式输出、上下文压缩）、MCP 客户端（调
console-be 自己的 `/mcp` 拿记忆工具）、会话持久化（磁盘 JSONL，可 resume）、模型网关
调用（BYOK：业务自配 baseUrl/model）。

**console-be 负责**：会话受理与排队、pi 进程生命周期（spawn/回收/驱逐/崩溃恢复）、
用户身份与鉴权、BYOK key 的存储与注入、审批策略、审计、SSE/WebSocket 转发、配额、
会话文件独占（flock）与历史快照。

**pi 明确不负责**：不提供任何 HTTP 服务端（不是 OpenAI Responses API 兼容端点）；
不做用户身份；不做多租户——一个 pi 进程只服务一个会话。

**关键澄清（业务侧「用 responses API」的语义）**：responses API 是 pi 作为 **LLM 客户端**
调用业务网关时使用的协议（`models.json` 里 `api: "openai-responses"`），不是 pi 对外
提供的接口。pi 对外的唯一编程接口是 stdio JSONL RPC（`pi --mode rpc`）。业务 BYOK
自配 baseUrl + model 完全支持，但它是「pi 去调谁的模型」，不是「谁能调 pi」。

## 运行形态

- **同容器子进程**：`pi` 二进制 COPY 进 console-be 镜像（musl 静态，56.7MB，无任何
  .so 依赖，基础镜像随意），console-be 直接 spawn，stdio 管道通信。**不做独立
  Deployment**——stdio 管道跨不了容器，独立进程还得自造 IPC，无收益。
- **只有发消息才 spawn**：建流、读历史、中断都只操作会话对象。会话对象（broker）
  按 `sessionId` 常驻，持有订阅者、事件序号、受理队列与当前进程指针；进程可随时被
  空闲回收或驱逐，会话对象不受影响。
- spawn 命令（每在途对话一个进程）：
  ```bash
  PI_CODING_AGENT_DIR=<per-user agent dir> \
  PI_DISABLE_SIDECAR_PREWARM=1 \
  pi --mode rpc --no-extensions --no-builtin-tools \
     --exclude-tools list_mcp_resources,list_mcp_resource_templates,read_mcp_resource \
     --no-approve \
     --session-dir <per-user sessions dir> \
     --session-id <chat session id> \
     --system-prompt <memory assistant prompt>
  ```
  `--system-prompt` 整段替换 pi 默认的 coding assistant 提示：默认提示会注入
  pi 的本地文档路径，并把 agent 描述成改代码的助手，不适合记忆问答场景。console-be
  内置一段“记忆空间助手”提示（只允许只读记忆工具、回答必须基于检索结果），可用
  `MEMOSD_CHAT_SYSTEM_PROMPT` 覆盖。
  **cwd 设为空目录**（每会话或全局空目录）：所有 pi 进程同 UID，不设隔离的话
  内建工具（bash/read/write 等）会读到其他用户的 agent 目录。`--no-builtin-tools`
  关闭全部内建编码工具（bash/read/write/edit/grep/find/ls）——chat agent 只用
  MCP 记忆工具。MCP 侧共 5 个工具（`add_message` / `search_memory` /
  `delete_memory` / `add_feedback` / `get_user_profile`）；`mcp.json` 的
  `toolExposure` 默认开 `search_memory` / `get_user_profile` / `add_message`；
  破坏性的 `delete_memory` / `add_feedback` 默认 `hidden`，需要时用 `MEMOSD_CHAT_TOOLS`
  显式列出。
- env（src/coding-agent/config.rs）：`PI_CODING_AGENT_DIR`（agent 目录，默认
  `~/.pi/agent`——**多用户必须每用户/每会话单独设**）、`PI_CODING_AGENT_SESSION_DIR`
  （会话 JSONL 目录）、`PI_OFFLINE`（离线）。**必须带
  `PI_DISABLE_SIDECAR_PREWARM=1`**：不带会尝试预启动 bun sidecar（集群无 bun，虽然
  lazy 设计下错误被忽略，但日志会有噪音且多一个无意义尝试）。
- 实测（WSL，同二进制）：RSS ≈ **45 MB**，25 线程，加 env 后**零子进程**。
- 健康检查：无独立端口。健康 = 进程存活 + 启动握手（`set_model` /
  `set_auto_compaction`）完成；由 piagent adapter 的进程监视承担，不做周期性
  `get_state` 探测，避免给每个空闲会话增加 RPC 噪音。`get_state` 只在两种时机调用：
  驱逐候选对账与单轮看门狗。
- 会话文件：`<base>/runtime/` 下放本服务自己的文件——`<sessionId>.lock`（排他
  flock）、`history.json`（每轮结束的 `get_messages` 快照）、`pending.jsonl`
  （已受理未完成的消息）。`sessions/` 仍只给 pi 用。

## 协议（stdio JSONL）

stdout/stdin 逐行 JSON。三类消息：

1. **command**（Go → pi）：`{"type":"<command>", ...}`。已实现命令（rpc_types.rs）：
   `prompt`（发消息）、`steer` / `follow_up` / `clear_queue`（插话/排队）、`abort` /
   `abort_bash` / `abort_retry`（取消）、`compact`（压缩上下文）、`get_state` /
   `get_messages` / `get_entries` / `get_tree` / `get_session_stats` /
   `get_last_assistant_text`、`get_available_models` / `set_model` / `cycle_model` /
   `set_thinking_level` / `get_available_thinking_levels`、`set_auto_compaction` /
   `set_auto_retry` / `set_steering_mode` / `set_follow_up_mode`、`set_session_name` /
   `new_session` / `switch_session` / `fork` / `clone` / `get_commands` / `export_html`。
2. **response**：与 command 的请求 id 关联（`type:"response"`）。
3. **event**（pi → Go，无需应答）：`AgentStart` / `AgentEnd` / `TurnStart` / `TurnEnd` /
   `MessageStart` / `MessageUpdate`（流式增量）/ `MessageEnd` / `ToolExecutionStart` /
   `ToolExecutionUpdate` / `ToolExecutionEnd`。

**必须应答的消息**：pi 会发 `extension_ui_request` 等请求等待宿主应答，**不回应会
挂起**。`--no-extensions` 下理论不出现在 chat 路径，但 adapter 仍要有兜底应答
（拒绝或空 ack），超时即 abort 进程。

**取消**：发 `abort` 命令；进程级兜底 = kill + 下次 `--resume`（会话在磁盘，安全）。

**SSE 透传（回应风险②）**：pi 侧没有 SSE——是逐行 JSONL。Go adapter 读到一行就
`write + flush` 一条 SSE 到前端即可，天然不缓冲。前端事件形状由 Go 定义（建议把
pi 的 event 原样包一层 `{type, sessionId, data}`），不要在 Go 层攒批。

adapter 对事件做一层投影，不是全量透传：

- `message_update` 只放行 `text_start` / `text_delta` / `text_end` 与
  `thinking_*` / `reasoning_*`。思考过程要展示给用户（前端折叠），toolcall 的增量
  仍然丢弃：它只是模型在拼工具参数，最终调用由 `tool_execution_*` 表达。
  真实对话里全量增量约 453KB，投影后约 40KB（不含 thinking）。
- 单条对外事件上限 256 KiB，超限丢弃并记 warn，不能让一条超大工具结果拖死连接。
- 其余事件（`agent_start` / `agent_end` / `agent_settled` / `turn_*` /
  `message_start` / `message_end` / `tool_execution_*`）原样透传。

## 协议样例（wire 级）

serde 约定：命令与事件都是 `{"type":"<snake_case 名>", 字段 camelCase }`
（rpc_types.rs / agent/types.rs 的 `tag="type"` + `rename_all`）；响应 envelope 固定
`{"id","type":"response","command","success","data"?,"error"?}`。以下样例字段全部取自
`rpc_types.rs` / `agent/types.rs` 实际定义。

**发 prompt（带插话语义）**：

```json
{"type":"prompt","message":"帮我查一下我的偏好记忆","streamingBehavior":"steer"}
```

**收事件流**（顺序示例，一次工具调用）：

```json
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"message_start","message":{...}}
{"type":"message_update","message":{...},"assistantMessageEvent":{"type":"text_delta","delta":"好的，我先检索..."}}
{"type":"tool_execution_start","toolCallId":"tc_1","toolName":"search_memory","args":{"query":"用户偏好"}}
{"type":"tool_execution_end","toolCallId":"tc_1","toolName":"search_memory","result":{...},"isError":false}
{"type":"turn_end"}
{"type":"agent_end"}
```

- 流式文本抽取：`message_update.assistantMessageEvent` 是上游 `AssistantMessageEvent`
  （`text_delta` 等增量事件）；`message` 是整消息快照。前端用 delta 渲染、快照对账。
- **检索可视化**：`tool_execution_start.toolName + args`、`tool_execution_end.result +
  isError`——按 `toolName == "search_memory"` 过滤即得「检索到什么记忆」。

**切换模型（BYOK 用户自选）**：

```json
{"type":"set_model","provider":"your-gateway","modelId":"deepseek-v4.1"}
```
或 spawn 时 `--model <provider>/<modelId>`；运行期切换用 setModel。

**get_state 的 data**（camelCase 全字段）：

```json
{"model":{...},"thinkingLevel":"...","isStreaming":false,"isCompacting":false,
 "steeringMode":"...","followUpMode":"...","sessionFile":"...","sessionId":"...",
 "sessionName":null,"autoCompactionEnabled":true,"messageCount":12,
 "pendingMessageCount":0}
```

**abort**：`{"type":"abort"}` → response success + `agent_end` 事件。

**extension_ui_response 兜底**：`{"type":"extension_ui_response","id":"<原请求id>","cancelled":true}`。

**usage**：`MessageEnd` / `AgentEnd` 事件的 message 字段内含 usage（input/output
tokens），Go 汇总后接控制台 Usage 页。

## BYOK（业务自配 baseUrl / model）

- **来源与存储**：用户在前端填 `baseUrl` / `model` / `apiKey` → console-be 校验后写入
  **该用户的 agent 目录** `<PI_CODING_AGENT_DIR>/models.json`。存储归 console-be：
  建议加密落库（或仅内存 + 会话内重建），pi 侧只消费文件。
- `models.json` 形状（camelCase，`model-config.rs` `ProviderConfig`）：
  ```json
  { "providers": { "<name>": {
      "baseUrl": "https://gateway.example.com/v1",
      "apiKey": "<secret>",
      "api": "openai-responses" } } }
  ```
  `api` 取值含 `openai-completions` / `openai-responses`（业务要 responses 就填这个）、
  `anthropic-messages` 等。**只填 `baseUrl` 时 pi 会请求 `{baseUrl}/models` 自动发现
  模型清单**（BYOK 发现），`--list-models` 可打印。
- **不进日志**：pi 侧凭据不落日志（错误只带状态码/request-id，仓库不变量）；Go 侧
  禁止打印 models.json 内容与 key。
- **超时/重试/成本**：pi 对 provider 的请求超时 30s、全局 HTTP idle 300s（网关挂起
  会被 idle 打断）；重试策略在 Go 配额层做（建议按用户设日预算上限，pi 不管钱）。

## 并发与容量

- **槽位按进程数计，但进程只由「发消息」触发**。建流、读历史、中断都不会 spawn；
  打开页面不占用槽位。
- **会话状态在磁盘，进程只是载体**：空闲可 kill，下条消息 respawn + `--resume`（亚秒
  级：spawn + 读 JSONL + MCP 连接，无 sidecar 后无重开销）。
- **空闲回收**：`MEMOSD_CHAT_IDLE_TTL`（默认 15 分钟）到点且无在途轮次时停止进程；
  订阅者不再阻止回收（订阅挂在会话 broker 上，不挂在进程上）。
- **准入顺序**：槽位满时先摘最久空闲、未在生成的会话，走快速路径（不发 abort，直接
  kill 进程组），等进程退出并释放会话锁后让出槽位，再准入下一条；只有全部槽位都在
  生成时才拒绝。发消息路径在收下之前先同步预检一次（无空位且无可驱逐会话 → 429），
  预检通过后仍可能被并发抢占，那种情况走 202 之后的 `chat_error`。容量判断与占位在
  同一临界区完成，并发准入不会突破 `MaxProcs`。准入与驱逐都是有界的，不会无限等待。
- **单会话串行**：同一会话一次只发一个 prompt，运行中的消息进入队列，`agent_settled`
  后自动续发。`prompt` 应答超时 3 分钟（压缩是同步 LLM 调用），超时不等于用户失败。
- **100 用户估算**：并发系数 20–30% → 20–30 个在途会话。进程数由 `MaxProcs` 限制，
  不再由打开页面的人数限制；12 个槽在空闲场景可以覆盖远多于 12 个日活用户，只在
  “12 轮同时生成”时才会 429。
- **内存预算**：dev 实测 pi 进程 RSS 空载 42 MB、压缩前后峰值 57 MB、P95 47 MB
  （`/proc/<pid>/status` 每分钟采样），128K 窗口下自动压缩会把上下文压在阈值以内。
  预算公式 `MaxProcs × P95_RSS + BFF 常驻 < limit × 0.8`；准入前读 cgroup v2
  `memory.current`，余量不足一个进程预算时直接 429，不让内核 OOM 挑受害者。
- **目录清理**：JSONL 会随轮次增长；按会话结束 + TTL 双条件归档/清理。历史快照与
  受理队列都在 `runtime/` 下，随会话目录一起清理。
- **会话锁**：`runtime/<sessionId>.lock` 排他 `flock`，进程确认退出后才释放；这保证
  驱逐、重拉、孤儿回收之间不会出现两个进程写同一份 JSONL。
- **孤儿进程清扫**：console-be 启动时按 cmdline 特征（`pi --mode rpc` + 同 UID）
  扫 /proc 定向回收上一代残留；运行期 spawn 记录 pid，正常路径显式 stop。
- **崩溃恢复**：pi 进程崩 = 该会话当前轮中断；adapter 检测 exit → 下次发消息重启 +
  `--resume` 同 session-id，用户看到一次可重试错误。会话文件本身有原子写。

## 工具与记忆检索

- pi 经 MCP 调 console-be 自己的 `/mcp`（已有 6 工具，golden schema）。agent 目录
  `mcp.json`：
  ```json
  { "mcpServers": { "memory-console": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:<port>/mcp",
      "exposure": "direct",
      "toolExposure": {
        "search_memory": "direct",
        "get_user_profile": "direct",
        "add_message": "direct",
        "update_memory": "direct",
        "delete_memory": "direct",
        "add_feedback": "direct"
      },
      "headers": {
        "Authorization": "Bearer ${MEMOS_MCP_TOKEN}",
        "X-Memos-Source": "dashboard-chat-agent"
      } } } }
  ```
  **必须显式 `exposure: "direct"`**：pi 默认的 codemode 暴露方式会让模型写 JS 调工具，
  事件里 `toolName` 只有 `codemode`，检索可视化拿不到真实工具与 query。direct 下事件名
  是 `mcp__memory_console__<tool>`。
  `${ENV}` 由 pi 在连接期展开（mcp/connect.rs 支持 headers 环境变量插值），
  **token 走 spawn env 注入，不落盘**。`X-Memos-Source` 是自定义审计头——/mcp
  两端都是我们的，用它区分 agent 流量与外部 MCP 调用，审计归属不混淆。
  同 Pod 内走 localhost；跨节点部署才需要真实域名。支持 stdio / streamable-http /
  SSE 三种传输（`src/mcp/transports/`）。
- **检索可视化 = ToolExecution 事件**：`ToolExecutionStart`（含工具名与参数）、
  `ToolExecutionEnd`（含结果）按工具名过滤 `search_memory` 就是「这句 query 检索到
  什么记忆」的可视化真值，前端直接渲染。单一事实来源，不会与 agent 实际用到的记忆
  脱节。
- **`MEMOS_MCP_TOKEN` 决策（首版）**：token = 用户所选项目的 apikey（产品语义：
  记忆空间 ≡ 项目空间，一个项目多个 apikey 任选）；当前默认开放读写六工具，支持明确要求的写入、纠正与删除。部署可通过MEMOSD_CHAT_TOOLS收紧；服务层按凭据强制限定项目范围。
- **项目前置校验（产品约束）**：chat 会话创建前 BFF 校验——未选项目 → 提示选择
  项目；项目无 apikey → 提示创建 apikey。有项目 + apikey 才 spawn pi（记忆与 agent
  绑定）。
- 「查询记忆」走同一 runtime 的 MCP 工具为主；若 dashboard 首屏想预取，Go 可以直接
  调数据面 REST（与 agent 无关的两条路径，互不影响）。

## 可观测

- pi 的 **stdout 是纯协议流，stderr 是诊断日志**——采集时必须分开，混流会污染协议。
- token/usage：`MessageEnd` / `AgentEnd` 事件携带 usage 字段（input/output tokens），
  Go 汇总后可接控制台既有 Usage 页。
- 错误映射建议：pi 侧错误以字符串 + 退出码呈现；Go adapter 定义错误码（进程崩溃 /
  RPC 超时 / provider 4xx/5xx 透传 / abort），provider 的 HTTP 错误语义原样透传给
  前端（BYOK 场景用户需要看到自己的网关报了什么）。

## 本地开发

**mac 不编译**（统一约束）。两种方式：

   静态二进制无 libc 需求，模拟也轻）。
2. 需要原生 arm64 mac 二进制时，由 rust-pi 侧在 CI/WSL 侧出产物（darwin 目标不在
   WSL 工具链内，需 GitHub Actions 或一台 mac runner——向 rust-pi owner 提需求）。

## 已知边界与风险

1. **prewarm 门控缺陷**（已留证）：`--no-extensions` + 零扩展配置下 RPC 启动仍会尝试
   预启动 bun sidecar；部署用 `PI_DISABLE_SIDECAR_PREWARM=1` 规避，rust-pi 侧待修。
2. **多副本**：会话在 Pod 本地盘 → 用户必须粘住同一 Pod（sticky 路由）或上共享存储。
   控制面当前单副本 + hostPath，与单副本假设一致；扩副本是后续工作。会话 flock 只在
   同一内核内有效，跨节点仍需共享存储或路由约束。
3. **并发派生值**（chord H24，与本接入无直接关系，仅登记）：多线程并发写 cell 时
   派生值最终态按 install 到达顺序，不承诺「并发写后必为最新」。
4. **单进程单会话**：不要试图一进程跑多会话；上游 v1.1.0 删除了实验性多会话 server
   栈，宿主自管进程是上游立场。

## 依赖

- 实现 `internal/adapter/piagent`，实现 `service/chat` 声明的 `Runtime` 端口；不 import
  service 或 transport。
- 只依赖标准库与 `domain` 的基础类型；进程、JSONL、SSE 转发全部在 adapter 内完成。
- 被 `service/chat` 与 `app` 依赖；`app` 负责按 `MEMOSD_CHAT_DIR` 是否配置决定是否接线。

## 契约

- 端口：`Runtime.Session(spec)` 只返回会话对象，不 spawn；`domain.ChatSession` 暴露
  `Submit` / `Abort` / `Messages` / `Subscribe`。spawn 只发生在 `Submit` 的派发路径上。
- 进程：每个会话一个 `pi --mode rpc` 子进程；spawn 参数与 env 见「运行形态」，命令块是
  实现必须逐项照抄的最小集合（`--no-builtin-tools`、空 cwd、`PI_CODING_AGENT_DIR`）。
- 协议：stdin/stdout 逐行 JSON。命令字段以 `rpc_types.rs` 为准：`prompt.message`、
  `prompt.streamingBehavior`、`abort`、`set_model{provider,modelId}`、
  `set_auto_compaction{enabled}`、`get_messages`、`get_state`；响应
  `{id,type:"response",command,success,data?,error?}`。
- 事件：`type` 为 snake_case（`agent_start` … `tool_execution_end`、`compaction_start` /
  `compaction_end`），`message_update` 只带增量与 `usage`；未知事件原样透传。
- 文件：`<agent_dir>/models.json`（`providers.<name>`，`contextWindow` / `maxTokens`
  必填且大于 0）、`<agent_dir>/mcp.json`（headers 用 `${ENV}` 注入）；
  `<base>/runtime/` 下是锁、历史快照与受理队列，全部临时文件 + rename 原子写。
- 不变量：一个进程只服务一个 session；同一会话同一时刻只有一个写者（flock）；
  stdout 只承载协议，stderr 只进日志；任何凭据不写日志、不拼进命令行参数；
  被 202 受理的消息要么执行、要么以可重试错误回到用户面前，不会静默消失。

## 错误与边界情况

- spawn 失败、二进制缺失：返回可映射为 503 的 typed error，不缓存失败进程。
- 会话锁被占用：短暂等待后重试一次；仍失败返回「会话正在恢复，请稍后重试」。孤儿进程只由启动时的 `SweepOrphans` 统一回收，不在锁冲突时 kill 一个仍在写会话的进程。
- RPC 请求超时：`prompt` 3 分钟（压缩是同步 LLM 调用）、`get_messages` 10s、
  `abort` 5s、启动握手每步 5s；`abort` 超时后 kill 进程组。
- 进程异常退出：向订阅者推 `chat_error{retryable:true}`，清理进程记录与 running 状态；
  下一条消息重新 spawn。排队的后续消息继续派发。
- 单轮看门狗：空闲 10 分钟、单轮总时长 20 分钟仍无 `agent_settled`，先用 `get_state` 对账
  （只收尾对账开始时的那条，避免把下一轮当成上一轮）；仍在生成才 kill，并把该轮放进
  `needs_retry` 后推可重试错误。
- `extension_ui_request` 等需应答消息：超时统一回复拒绝，避免挂起。
- 单行超过 16 MiB：记录并丢弃该行，不断流。
- 宿主重启：启动时按 cmdline + UID 回收残留 `pi --mode rpc` 进程（仅 linux 实现）；
  遗留 `pending.jsonl` 不自动重发，转为 `needs_retry` 告知用户。

## 测试与验证

- 单元：fake pi 辅助进程（`GO_WANT_FAKE_PI`）覆盖 spawn 与配置文件生成、命令编码、
  事件解码、响应关联、超时、异常退出、MaxProcs；并发 `Session` 只起一个进程；
  满槽驱逐而非 429；`flock` 单写者；`Messages`/`Abort` 无进程可用；快照原子替换；
  订阅者溢出后重连补齐；漏 `agent_settled` 由看门狗恢复；受理消息不静默丢失。
- 集群：dev 部署后用 superduck 走完整动作序列并留截图；真实 pi 的
  `-tags=integration` 用例按需补。

## 参考来源

  `json-event.rs` 投影。
  形状与 `contextWindow` / `maxTokens` 校验。
  与 `${ENV}` 解析。
- 本文档的后端契约与前置校验见 [chat](chat.md)。

## 交付物

- rust-pi 仓库 `dist/pi-1.3.0-linux-amd64/`：`pi`（musl 静态，56.7MB，sha256
  `c779edd6c3e0a0111e340008061cb76aa11eb949e9ddfdac40ed90e92e3b33ee`）+ agent-dir 模板
  + 部署说明；另有同名 tar.gz（24.6MB）。
- 实测基线：RPC 模式 RSS 45MB / 25 线程 / 零子进程；全量测试 3646 passed / 0 failed。

## 实现记录

See the public version-control history.
## 助手人格与参考问题（2026-10-31，implemented）

默认system prompt来自内嵌soul.md，MEMOSD_CHAT_SYSTEM_PROMPT覆盖能力保持。设置友好直接、结论在前、适度详略的个人助手人格；只声称实际工具支持的能力，不提前承诺异步写入完成。可在正文尾部输出0至3个同语言的用户视角后续问题，放入`<graph-memory-followups>` JSON数组；前端解析为参考问题，不把协议块显示/复制为回答。未返回时不伪造个性化内容。

## 个人助手能力补齐（2026-10-31，accepted）

- 新增 `update_memory` / `POST /memory/update`，只接受memory_id、content与可选title；身份与作用域来自凭据。控制面先查归属，调用已有 `/product/update_memory` 后读回验证正文、标题与版本，不把受理当更新成功。重复相同正文不递增版本，禁止删除再新增模拟修改。
- 更新复用底座的嵌入重算和history，返回真实记忆投影供聊天日志展开。当前项目外、空正文、已删除或不存在记录必须拒绝。
- 个人助手默认开放六个记忆工具，具体删改仅按当前用户明确意图；检索内容不构成授权。环境仍可覆盖工具白名单。纠正反馈需检查目标ID归属，成功回执不代表产生了修改。
- 回答评分仍独立持久化；用户可在回答旁明确输入纠正，作为用户消息进入已有聊天链路，绝不由点赞/点踩推断事实。
- 验证：服务归属/no-op/上游失败、适配器请求作用域与错误、MCP/REST契约、配置工具暴露，以及dev任务项目真实写查改版本删闭环。




引用采用真实结果的完整ID，推荐 `[memory:call_id/memory_id]`，仅在该轮实际工具结果中存在时使用。前端按该轮不可变快照验证调用和ID并显示稳定序号，思考渠道已有的标记和回答正文使用同一映射；不要求模型额外输出思考或在旧文本中猜造引用。兼容参考站 `[序号:8位ID前缀]` 时必须唯一命中该轮快照，前缀冲突不生成链接。无匹配/未返回引用时不伪造。

具体个人问题优先一次限定范围的search_memory（通常top_k6，按实际需要决定）；“记得我什么”这类概览优先get_user_profile，不同时做泛化搜索和整份画像。画像分页是上下文读取而非向量排名，不给它编造相关度；界面保留真实去重总数，标明检索命中和额外画像，分批6张展示。参考问题继续由同一次LLM回答动态提供0至3个，不增加专门模型调用，不用固定题库补齐。



## 2026-11-01 标题与反馈补齐（implemented）

- 反馈现有SQLite评分表增加意见、对话ID、回答摘录；GET/PUT同一路径扩展可选字段，旧客户端保持兼容。文字独立保存成功后才可发送纠正消息；发送失败仍保留已保存意见。取消评分删除该条反馈。项目、账号及对话归属均校验，不启动模型、不将普通评分自动写为记忆。
- 首轮最终回答在同一次生成末尾增加JSON字符串元数据 `<graph-memory-title>"简短主题"</graph-memory-title>`；无额外模型请求。前端过滤元数据并刷新历史标题；参考问题可在标题前后出现。无有效标题保留未命名，不用截取用户输入冒充模型生成。
- Runtime在正常历史快照落盘后读取最早有效最终assistant标题，通过窄存储端口仅更新当前项目/账号/对话且标题为空的行；不依赖浏览器连接。已有显式标题不覆盖，旧未命名对话在下一次真实回复时可补全。后台不扫描旧会话或自动发消息。
- SQLite只保存对话元数据；Pi仍唯一保存消息与上下文，session ID和路径不随标题变化。增量迁移仅加反馈列，旧镜像忽略新列即可回滚；新标题不要求修改Pi分发文件或数据面。

## 2026-11-02 工具暴露与冷启动边界

不纳入或修改rust-pi源码，不更换锁定二进制。依据[Pi官方MCP文档](https://pi.dev/docs/latest/mcp)及固定上游1.3.0的[对应文档](https://github.com/earendil-works/pi/blob/71023aec1/packages/coding-agent/docs/mcp.md)，少量常用工具使用`exposure: direct`及逐工具direct白名单，未授权工具hidden，不经过deferred/tool_search。显式设置`autoEnableCodemode: false`关闭自动codemode；它不是关闭后台MCP连接的开关。

原接入已经使用direct，因此不能把再次声明direct当作冷启动根因修复。官方文档描述首prompt对direct服务器最多等待10秒；当前使用的Rust分发是否实现同一就绪等待，必须通过K8s锁定二进制验证，不能仅凭配置单测或本机另一版本源码认定。内嵌人格要求真实原生调用，禁止工具调用格式出现在回答正文或伪造保存结果。

前端未观察到真实写入且回答已结束时提供显式重新保存；只有用户点击才发送恢复消息，不能把等待/受理当完成。真实冷启动模型验收仍需本任务另行授权，不自动重复模型请求。
