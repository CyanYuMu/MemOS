# P3：管理、审阅、隔离评测与发布准入

日期：2026-11-08。依据[冻结设计](skill-extraction-design.md)第 9–10、14 节及 [P2 验收](skill-prototype-p2.md)。本文件先提交范围和契约，实际结果随后追加。

## 1 目标与边界

接通 React 审阅页 → 已认证 Go 控制面 → Python Skill 权威服务 → 独立 Neo4j 管理事务，并将固定候选接入已有 Claude Code 受控运行器。

本轮交付四项可运行能力：项目内候选/出处/运行/发布状态查询；未发布候选的显式隔离评测；绑定精确审阅包、状态序号和验证报告的人工发布事务；正常使用路径上的 Release 准入及运行结束复核。

**不能把 P2 候选改成已批准正文。** candidate.json 永远保持不可变；审阅、验证、挑战、发布和使用资格作为独立管理记录。真实 P2 候选已有验收遗漏等待审事项，本轮不会以自动程序冒充人工批准。

本轮真实 Claude 验证采用已注册 dev 任务，明确标注 `isolated_dev`。这些验证可以证明固定版本加载、隔离、回收和外部验收链，不能代替 P4 的独立任务及三组效果报告，也不能单独满足普通发布条件。发布成功/并发路径使用明确标记的合成管理夹具测试；真实候选若证据不足，应在界面和普通加载路径被拒绝。

P4 才开展正式 holdout 运行与对照实验；P5 才实现持续来源监测和完整依赖失效传播。本轮提供显式挑战、暂停/撤回和加载复核的基础事务，不声称能远程清除已经进入 Agent 上下文的旧内容。

## 2 分支与代码保护

| 工作区 | 分支 | 起点 |
| --- | --- | --- |
| Python 主工作区 | `codex/skill-prototype-p3` | P2 `b5620437` |
| Go 控制面工作区 | `codex/skill-prototype-p3-be` | be `31e303bd` |
| React 前端工作区 | `codex/skill-prototype-p3-fe` | fe `e2a9df17` |

各自作中文提交，不跨分支复制整套项目。保留原有未跟踪 `bin/`。不修改 `skill_adapter/` 下任何 Python 文件、不修改 `skill_experience/evidence.py`、`skill_evaluation/{verifier,snapshots,tasks,cases}.py` 或 Python 依赖环境；这些均参与既有来源证据摘要。在新 `skill_management/` 包中复用公开接口。

Go 与前端按各自 README/Makefile/锁文件构建。Go 模块声明的工具链与本机默认版本不同，使用模块要求的工具链，不擅自降低 go.mod 版本。

## 3 架构与身份

1. React 使用既有同源会话和项目选择，沿统一 API 信封请求。页面只展示能力，权限仍由服务端裁定。
2. Go 沿 dashboard wrap 执行会话认证、`InBodyStrict` 项目授权和写审计；传递可信 `p.Actor()` 与能力，不转发浏览器伪造的 actor/header。查看为 view，评测为 write，审阅/发布/状态为 manage。
3. Python Skill 服务为独立可选进程，不改变原 Memory API。服务间固定 Bearer 凭据只在服务器配置中。可信项目 ID 到 Skill Graph 项目的映射显式登记，不能按名称猜测。
4. 候选只从宿主登记的不可变工件路径加载。HTTP 不接收任意文件路径、Cypher、命令、provider 凭据或任意任务代码。原始 oracle、完整运行目录、连接配置和 Agent 凭据不通过前端下载。
5. Neo4j 为 Skill 状态唯一权威。Go SQLite 只保留既有身份/项目/审计职责，不复制发布真相。大工件仍保存在仓库外固定根目录。

Python HTTP 入口为 `POST /v1/skills/<action>`，请求字段使用控制面的 camelCase；内嵌 `candidate` 保持 P2 规范字段不改写。服务鉴权要求 `Authorization: Bearer <service-token>`、`X-Skill-Actor`、`X-Skill-Capability`（view/write/manage）及 `X-Skill-Project`；这些头由 Go 基于已认证上下文生成。

响应为 `{data: ...}`；错误为 `{error:{code,message,details}}`，仅固定安全错误/摘要，HTTP 使用 400/401/403/404/409/422/503。Go 映射为既有 `{code,data,message}` 信封，不透传上游端点/堆栈。所有请求必须含显式 `projectId`。

## 4 最小 API 契约

| Go 路由 `/api/v1/skills/…` | Python action | 能力 | 除 projectId 外的输入 |
| --- | --- | --- | --- |
| `list` | `list` | view | 可选 query |
| `detail` | `detail` | view | skillId, revision |
| `evidence` | `evidence` | view | skillId, revision, ruleId |
| `evaluations/start` | `evaluate` | write | skillId, revision, caseId, operationId |
| `jobs/detail` | `job` | view | jobId |
| `reviews/prepare` | `review_prepare` | manage | skillId, revision |
| `reviews/decide` | `review_decide` | manage | skillId, revision, expectedSequence, operationId, decision（needs_evidence/block）, reason |
| `publish` | `publish` | manage | skillId, revision, packageId, packageHash, expectedSequence, operationId, reason |
| `status` | `status` | manage | skillId, revision, expectedSequence, operationId, action（pause/withdraw）, reason |
| `challenges` | `challenge` | manage | skillId, revision, expectedSequence, operationId, reason |
| `load-preview` | `load_preview` | view | skillId, revision, caseId, mode（normal） |

同一 operationId 与相同请求幂等；同 ID 不同内容返回冲突。未知字段、任务、状态、空 reason、缺失项目和跨项目身份均拒绝。评测返回 `jobId/status=queued`，不会在 Go HTTP handler 中等待 Claude 完成。

列表返回 `{items:[{skillId,revision,title,purpose,tags,state,sequence,readiness:{status,reasons},ruleCount,validationCount}],total}`。详情返回 `{projectId,candidate,management,validations,jobs,releases,caseOptions}`；management 至少包含 `state/sequence/readiness/challenges`，caseOptions 只列服务器许可的 dev case ID/标题。证据返回 `{rule,facts}`，由固定引用在已核验 packet 内定位，不能读取模型指定 URI。

job 返回 `{jobId,status,skillId,revision,caseId,mode,taskResult,skillUsage,validationId,errorCode}`。任务结果与 Skill 使用状态分开：任务测试通过不等于规则正确应用；SDK 完成也不等于独立验收通过。前端轮询终态，失败、未知和加载未证实均可见。

审阅包返回 `{packageId,packageHash,sequence,skillId,revision,readiness,validationIds,sourceWatermark,contentHash,reviewChecklist}`。发布/状态返回新的管理状态、sequence、releaseId（如有）；409 展示冲突并要求重新读取，不能自动重试批准。

## 5 管理事务与发布条件

管理记录使用独立内部标签/追加记录，核心图继续使用九类节点；运行/验证可增加 dev 类型 CaseRevision 和明确 kind 的 Validation，不能伪装成 source 或收益评估。Release 是新批准记录，不能覆盖 SkillRevision 正文。P2 只接受候选的通用图写入口不获得任意发布权限。

每个项目写入先竞争与图仓储一致的项目锁；发布、挑战、审阅处置、状态变化和幂等操作记录在同一事务中提交。所有发布材料绑定固定候选/导出摘要、完整 Validation ID、来源水位、当前 sequence 和权限主体。序号变化、内容变化、失效来源、未决挑战、未知依赖或验证不足返回冲突/阻断，不部分写入 Release 或推荐指针。

普通发布最低要求：完整候选/来源核验、可支持普通范围的独立 Validation、无未决实质挑战、全部必需分支与边界覆盖、人工 review reason 与精确审阅包确认。`isolated_dev` 和结构报告不能冒充 `independent_task`；真实三组效果报告需 P4 提供。当前原生适配器不支持非空 REQUIRES，因此遇到它们明确阻断，不静默遗漏闭包。

P2 已知待审问题以管理挑战登记，原候选不改写。补证据/阻断决定追加记录并改变 sequence。暂停、撤回和新挑战使后续 normal 准入失效；不删除历史批准记录。P3 不提供跳过验证、客户端自报评测通过或自动批准接口。

## 6 候选隔离评测与 Claude 准入

新增规范到原生 Skill 的确定性投影，保留规则、条件、分支、退出、约束和验收内容，记录 canonical revision、exporter version、runtime_alias 和 export_hash。不导出来源原始日志、审阅包、连接配置或 oracle。超长 rule_id、动态内容、未知依赖等现有适配器不支持的输入 fail closed；不偷偷改名或截断规则。

候选评测由宿主创建固定候选/任务/预算授权，服务进程异步执行已有 `run_case`，仅允许已注册 dev。没有 source 或 holdout 任意路径参数。继续复用 OS sandbox、visible-check broker、事件记录、最终快照及外部 verifier；结果重新 `check_receipt`，严格区分通过、失败和采集未知。评测产物不会进入 P1b source 入库。

normal 路径在启动前检查匹配 Release、固定工件、当前状态/sequence、任务范围、依赖与源水位；未发布候选直接拒绝。完成时再次核对许可，若期间变更，运行标记需复核，不能当作可用 Skill 的验收。旧 Runner/Hook 没有状态回查扩展，本轮只承诺**阻止新加载和最终结果准入**，不声称可中途撤去模型上下文或拦截每个工具动作。后续增强须另版本化适配器并处理旧证据迁移。

## 7 前端与可见运行

React 增加独立 Skill 页面及导航，包含搜索/状态列表、条件和规则、逐规则证据、评测发起与运行状态、固定审阅包、退回/阻断/发布、暂停/撤回以及加载拒绝原因。中文文案分别表示结构通过、任务通过、规则使用未审查和发布许可；不会用统一绿色“成功”替代全部状态。

权限未知时按钮默认禁用；viewer 不可评测，editor 不可批准。发布先展示不可变提案及摘要，再由管理者明确提交；冲突重新读取。证据/正文以文本安全渲染，不执行 Markdown 动态扩展。界面错误、无数据、等待、已拒绝和权限不足均有可见状态。

用户要求所有后续服务启动和日志在 iTerm 中可见。当前 CUA 明确禁止访问 iTerm，不绕过该限制；完成脚本后请用户在 iTerm 运行具体命令。启动器使用仓库外配置，并聚合带服务名前缀的日志/独立日志文件；凭据不打印。现有 Memory/Docker 可复用并接入日志。前端实际联调在可见浏览器中打开，不以 mock 页面代替真实 Go/Python 链路。

## 8 工作包与验收

| 包 | 交付 | 验收 |
| --- | --- | --- |
| P3-0 | 本文、三工作区分支、协议 | 文档先提交；保护旧证据边界 |
| P3-1 | Python 管理领域/Neo4j 事务 | 不可变登记、状态序号、幂等、并发挑战、跨项目和发布条件测试 |
| P3-2 | 独立 HTTP API 与异步评测、准入 | 鉴权/白名单、完整失败记录、服务重启后的未完成作业不误报成功 |
| P3-3 | Go 代理/能力/审计/OpenAPI | viewer/editor/owner、伪造 actor、上游错误、409 和超时；既有控制面回归 |
| P3-4 | React 页面/状态/发布提案 | 真实返回渲染、权限禁用、冲突不重试、失败可见、build/type/lint/tests |
| P3-5 | iTerm 脚本、浏览器及真实 Claude 演示 | 新服务日志可见；真实候选隔离加载与回收；未发布 normal 拒绝；合成发布事务与真实图验证 |

最终必须区分三种证据：真实 P2 候选的管理/拒绝及 Claude dev 执行；合成管理测试的批准/竞态；尚未进行的 P4 独立效果实验。Go/前端/Python 各作中文提交，最终文档记录具体版本、命令、通过数、实际结果、限制及 P4 入口。未经用户实际人工裁决，不给真实候选生成 Release。

## 9 实际结果

待执行后补充，不预填成功。
