# P3：管理、审阅、隔离评测与发布准入

日期：2026-11-08。依据[冻结设计](skill-extraction-design.md)第 9–10、14 节及 [P2 验收](skill-prototype-p2.md)。范围和契约先于实现提交；第 10 节记录实际验收结果与后续边界。

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

真实联调补充：原生工具的 `evidence_refs` 只接受宿主事件 ID，文件行号或检查序号不能冒充事件关联。当前工具没有向 Agent 暴露这些 ID；`management-native-3` 因此要求每次声明只提交 `rule_id/phase/observation/decision` 四个字段，省略引用字段，保留 `declared_only/unreviewed`，由后续人工及独立任务判断使用是否正确。v2 的条件性空引用提示在真实运行中仍出现检查序号误用，相关失败记录保留。此约束属于适配器输出契约，不修改候选正文或来源证据。

导出格式修订使用新的 exportHash/projectionVersion。宿主专用 `bind_export` 在无活动作业、从未发布的版本上原子登记新摘要并增加 sequence，审计保存前后版本；旧 Job/Validation 不覆盖。浏览器没有此接口，已发布、暂停、撤回或存在历史 Release 的版本不能静默替换导出。服务启动先恢复中断作业，再显式绑定当前投影；已批准版本的导出升级需要单独的迁移设计。

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

## 9 运行配置与部署边界

启动 Python 服务：`.venv/bin/python -m memos.skill_management serve --config <私有配置文件>`；`check` 命令仅执行配置、候选导入、真实来源回读和作业恢复检查后退出。两者均持有同一宿主的全局进程锁，防止地址别名或不同配置文件触发第二个恢复进程。P3 不支持多宿主副本；扩展前必须增加数据库租约及任务所有权。

私有服务 JSON 必须为 `0600`，字段如下。真实密码、端点、工件路径只保存在仓库外。

| 字段 | 内容 |
| --- | --- |
| serviceToken | 至少 32 字符、无空白的服务间凭据 |
| listen | host 固定 `127.0.0.1`，显式 port |
| graph | uri、user、password、database、memory_database_id；复用 P2 的独立图身份校验 |
| memory | url、ledger、snapshots，可选 token；必须指向已有 P1b 注册账本 |
| projects | projectId、graphProject、candidates；外部项目与图项目均不允许重复 |
| candidates 每项 | directory、challenges；只能由宿主配置，不接受 HTTP 路径 |
| runtime | cliPath、固定 model、timeoutSeconds、maxTurns、maxBudgetUsd、providerEnvironment |
| providerEnvironment | 仅支持旧适配器的四个 provider 凭据/端点字段，不继承全局插件和 Hook |
| artifactsRoot | 仓库外绝对路径，存放每个 job 的运行工件 |

`scripts/dev/skill_p3_console.py --config <私有控制台配置>` 在调用它的终端启动 Python、Go、Vite，输出 `[skill]`、`[go]`、`[web]` 日志并保存分服务日志。控制台配置包含 serviceConfig、goWorktree、webWorktree、projectId、logRoot、goEnvironment，以及可选 followLogs 和 dockerContainers。它不安装依赖；Go 先执行 `make check`，前端按锁文件安装后执行 `pnpm verify`。Ctrl+C 只关闭本次创建的进程，原有 Memory 和存储容器保持运行。

Go 使用 `MEMOS_SKILL_API_BASE_URL`、`MEMOS_SKILL_API_TOKEN` 接入服务。前端使用真实 Go 代理启动，路径为 `/cn/skills`。控制台启动器通过既有管理员 API 创建有真实 owner 关系的演示项目，不直接修改身份数据库。登录密码只保存在私有配置，不打印到日志。

新模块的职责：`domain.py` 定义验证/发布条件；`repository.py` 负责图事务与作业账本；`runtime.py` 负责固定候选投影、运行前后准入及凭条复验；`service.py` 负责宿主编排与单执行槽；`api.py` 负责服务身份、能力和严格命令边界；`config.py`/`__main__.py` 负责私有配置、来源回读、进程互斥与启动。

来源片段在浏览器显示为明确标记的副本：隐藏文本内嵌的宿主 `file://` 地址及私有路径，`display_redacted=true` 表示正文已脱敏，`value_sha256` 始终指向原始值。原始事实和封存候选不变。P3 扩展的 Validation/Release 由 `ManagementRepository` 解码和追溯；P2 通用图写入口仍不具备发布权。

作业无自动重试；同 operationId 从持久记录重放，不因另一个作业正在执行而重复启动。服务重启将未完成作业标为 unknown。新的作业、验证或挑战改变 sequence，运行结果若观察到期间的变化，需要复核。源水位、候选内容、导出摘要、Release 和最终 receipt 均分别核验。

## 10 实际结果

### 10.1 工程验证

Python 相关回归（含旧 P0–P2 和 P3）为 **786 passed in 86.67s**，其中 **19 项真实 Neo4j 测试**；P3 专项 **85 passed in 4.97s**。最后将规则声明格式收紧为四字段后，运行器专项再次 **12 passed**。Ruff 和提交钩子通过；为保持既有来源环境，未运行会改动环境/锁文件的 Poetry install/lock。旧适配器、验收核心和 Python 依赖环境均未修改。

Go `make check` 通过，含 lint、vet、全库 race/乱序测试、tidy、文档、部署检查和构建；新增 13 个顶层测试覆盖角色权限、会话、代理、安全错误、审计和项目隔离。React 完整 `pnpm verify` 通过，含 135 项测试、typecheck、lint、格式、自测和生产构建，Skill 专项 35 项。构建保留现有大 chunk 提示。

真实终端启动由用户在 iTerm 执行准备好的短启动器。联调发现并修复本机检查误用代理、TIME_WAIT 被当成端口占用、项目字典非成功信封/模糊匹配等问题；启动器新增 11 项隔离回归。服务日志带名称显示，并在仓库外分别保存。

### 10.2 真实链路结果

Go → Python 已实际验证：候选详情、6 条规则的来源片段、脱敏标记均返回成功；未登记的隐藏任务返回 400；未发布候选 normal 准入返回 allowed=false。前端已在用户 Chrome 中展示真实项目、候选、来源片段和运行状态。

Claude 开发运行记录全部保留，均未进入正式收益评估：第一次因为私有 provider 基址误包含 `/v1/messages` 而失败；第二次补丁外部验收通过，但规则声明误用了文件行号；第三次补丁验收也通过并记录 3 条有效声明，但两条声明误用了检查序号。两次声明不完整的运行均保留 unknown / partial，没有放宽采集器把它们转为成功。

第四次真实运行在 `management-native-3` 下通过。CLI 为 Claude Code 2.1.296，固定请求模型为 `claude-opus-5-5`，任务仍为 `dev-tenant-isolation`；这是适配调试后的开发验证，不是新的独立样本。原生执行耗时 108.844 秒，CLI 报告费用约 0.2250812 USD（非账单核对值），退出码为 0。

| 核验项 | 实际值 |
| --- | --- |
| jobId | `2674085e71ac203b4f50c09b65d139bfe3035b78bee70f2b93d8351570e359d1` |
| canonical revision | `9a3b0c13435bbe8f4c578b6956307e0ea9f9a0f9c1327fbd8550db0cd8fd455e` |
| exportHash | `ddc95b60aa545762683847edcbe628c7d4ada09a371d80bf3dd1481546f1608c` |
| 最终补丁 | 外部验收通过；receipt 与 run、attempt 和最终补丁快照绑定，复验通过 |
| Skill 加载 | `verified`，固定版本及原生展开已核验 |
| 事件采集 | `complete`，157 个事件，无采集问题 |
| 规则声明 | 5 条有效声明，`usageReview=unreviewed` |
| 独立分支/边界覆盖 | 均为空；开发运行不计入正式覆盖 |
| 结束时准入复核 | `allowed`，仅本次候选隔离执行 |

五条声明对应 `confirm_contract`、`repair_scoped_reads`、`verify_scoped_reads`、`repair_dream_update`、`verify_dream_update`。声明完整性只能证明记录符合契约，不能证明每条规则选择和执行正确；`handle_unresolved` 未在本次声明，未声称覆盖该分支。

浏览器实际展示最新通过作业及此前三次 unknown 记录、当前导出摘要和固定审阅包。审阅包绑定 sequence=14、全部 4 个开发 Validation 和来源水位，状态为 `NEEDS_EVIDENCE`，原因为 `open_challenges`、`validation_unknown`、`independent_validation_missing`；19 个规则分支的独立覆盖仍为零。发布确认控件禁用，普通加载仍被拒绝。经真实 Go → Python 发起的拒绝测试返回 **HTTP 422 / not_ready**，随后回读 **Release 数为 0**，没有自动批准真实候选。

独立 Neo4j 经 `ManagementRepository.inspect()` 回读为 **33 个核心节点、57 条边**：包含 1 个 Skill、1 个 SkillRevision、6 个 Rule、3 个 MemorySnapshot、3 个 CaseRevision、7 个 Run、7 个 EvidenceRevision、5 个 Validation，未包含 Release。5 个 Validation 分别是 P2 结构检查与本轮 4 次开发运行。内部管理账本不计入上述核心图数量。

实际报告保存在仓库外实验目录：`development-runs.json` 保留四次运行及费用；`projection-v3.json` 固定最终导出；`final-management.json`、`publication-rejection.json`、`graph-summary.json` 保存管理/拒绝/图回读；`verification-summary.json` 汇总工程与真实验证。浏览器截图和分服务日志也在该目录。凭据、宿主连接信息和原始运行工件不提交到仓库。

### 10.3 提交与验收结论

Python 依次提交范围文档 `d235b808`、管理与隔离执行 `f59ffc94`、启动与脱敏修复 `2889461f`、声明契约与导出版本审计 `6d0a85ff`、四字段声明约束 `1d281caa`。Go 实现为 `1c5010b8`；前端审阅页为 `18fb7d53`，导出绑定展示为 `9710a1f0`。均保留在第 2 节对应分支，无推送或合并。

P3 的管理接口、真实审阅界面、候选隔离执行、人工发布事务和加载准入基础能力完成本地验收。批准成功、幂等、并发挑战与撤回等路径由明确标记的管理测试夹具和真实 Neo4j 测试覆盖；真实候选只验证了管理、隔离运行及拒绝，尚未发布，也尚未证明收益。

### 10.4 P4 入口与尚未交付内容

1. **修订候选语义。** 对验收清单遗漏、健康无需修改分支和修复预算三个挑战作人工判断，生成新的不可变候选版本；不改写 P2 的封存正文和来源证据。
2. **定义历史结果处置。** 当前 readiness 保守地保留全部历史 unknown/failed 的阻断理由；一次成功不会自动消除旧未知记录。P4 需明确新导出验证与旧记录的审阅/替代规则，或对新候选建立新的验证集合，仍保留完整审计，不能覆盖旧报告。
3. **接入正式任务注册和评分。** 当前 HTTP 评测、加载预览及运行器只接受宿主登记的 dev case。P4 要增加受控 holdout/普通任务注册、独立 Validation 的可信生成、逐规则分支和边界评分；不得开放任意代码或客户端报告上传。
4. **执行三组固定版本实验。** 按冻结协议比较无经验、原始 Memory 经验、提炼 Skill，记录逐任务通过/失败/未知、正确修复与正确退出、质量、耗时和成本。开发调试的四次运行不能替代这组实验。
5. **完成真实人工发布。** 独立证据满足要求后重新生成精确审阅包，由人审阅并确认，才能创建真实 Release、验证普通使用准入。当前非空 REQUIRES 仍阻断；已发布版本的导出迁移、多宿主调度、持续监测与完整失效传播需后续设计，最后两项演进能力按 P5 推进。
