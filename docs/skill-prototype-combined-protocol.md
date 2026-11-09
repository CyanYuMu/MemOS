# 42-A4e-2/3 组合执行协议与证据接入

日期：2026-11-09。开发基线 `4a95fbf8`，分支 `codex/skill-prototype-combined-protocol`。本合同先于实现提交。当前 C1 资产自检已完成，真人覆盖为 3/22；本文件的执行协议、实际运行和 schema 3 尚待实现验收。

## 1 范围与顺序

本增量把已冻结的 `combined-c1-v1` 从资产自检接入受控 Claude 执行、固定中间状态、Neo4j 事实与浏览器人工审阅。目标是证明可验证的整次任务过程，不要求模型为了取得分支覆盖而额外编辑或检查。

顺序：新 verifier/broker/资格矩阵 → 固定 runner 与过程重算 → schema 3 collector/治理/UI → 无 Skill 校准与固定候选真实运行 → 事实导入和人工逐分支审阅。随后按组合扩展合同推进 C2、健康、停止和工具故障，处理剩余挑战与覆盖，再实现 42-B 来源/工具失效传播和新版发布；这些完成后才进入 P5。

本轮首批最多两个 Agent 会话：C1 none 校准和 C1 fixed-skill。校准不通过则停止后续会话；正式失败完整保留。不得为凑齐中间分支重复调用模型、改写旧报告或提示正确修复顺序。该批是协议与过程验证，不是新增收益对照实验。

## 2 冻结与版本

不修改旧 `skill_adapter`、P4/P4.1、`skill_branch_validation`、旧经验 evidence、schema 1 domain/collector 或管理投影。A4e-1 的 tasks/combined/selftest/CLI/锁也保持字节不变。新增协议入口独立于资产 CLI；新增实现放在 `skill_branch_extensions` 的新文件。仓库治理仅追加显式 schema 3 分派，schema 1/2 继续使用原合同。

协议分别命名为 `extension-receipt-1`、`extension-visible-1`、`extension-visible-result-1`、`extension-qualification-1`、`extension-process-1`、`extension-claude-1`、`extension-review-1`。任务身份绑定选定套件的完整 `suiteDigest` 和显式检查映射；实现摘要绑定对应执行逻辑与冻结通用依赖，不把未来全部套件目录纳入身份。数据加载后的完整套件摘要必须在执行前后相同，不能仅信任套件 ID。

未来套件可以沿用相同结果协议的前提是仍满足此合同；健康、停止和故障不属于当前核心修复结果类别，不能通过添加任务名称绕过类型校验。已封存协议的行为变化需新版本，不原地破坏旧凭条/事实回读。

## 3 凭条、公开结果与资格

`verify_suite(suite_id, candidate, artifact_root, run_id, attempt_id, timeout, cancellation_event, secret_values)` 返回既有 VerificationResult；`check_receipt` 返回 ReceiptCheck。凭条绑定套件身份、固定映射、完整候选快照、受信 oracle、环境、执行限制、程序/profile/工件、导入路径、检查顺序和每组状态。状态 passed/failed/error 严格区分；失败、保护文件变化、资源异常和损坏工件不能成为通过。回读重新构建固定 phase 并使用冻结工件核验器，替换补丁、重新计算外层摘要后篡改程序/profile/记录仍拒绝。

新 broker 保留固定 `run_visible_checks` 动作与原 socket/context 接口，不接受程序、路径和任意参数。每次先封存完整工作区，再在只读、无网络、无子进程的执行环境中运行固定公开程序。公开结果不包含 oracle、参考解、隐藏检查及其路径。

旧 recording_server 只允许六个标准字段及 evidence_ref，因此返回信封保持：exit_code、stdout、stderr、truncated、timed_out、execution_error、evidence_ref（check_index/candidate_snapshot）。stdout 是宿主从严格核验后的公开 report.json 生成的规范 JSON，不冒充原进程输出；原 stdout/stderr 独立保留为执行工件。内容为 schemaVersion=1、protocol=extension-visible-result-1、suiteId/suiteDigest/taskId、checkMapping、有序 checks、groupStatus 和整体 status。正常 exit 0 对应全通过，exit 1 对应业务失败；异常必须标 execution_error，不能以空输出或零退出码通过。`decode_public_result` 验证完整结构、重复键、映射、状态与退出码。

每条公开记录绑定检查序号、候选快照、套件/实现、完整执行工件及实际返回。回读结果要与 Claude 原生 tool_started/tool_completed 的同一调用完全匹配；只存在宿主成功记录而客户端未收到，视为送达缺口。

`audit_protocol(suite_id, output, ...)` 执行六变体的六次隐藏验收和六次真实 broker/client 检查。`require_qualification(directory, suite_id=...)` 重算固定完整矩阵、变体内容、凭条、公开工件与保留客户端响应。必须保持同一套件/实现/环境，删项、重复、错变体、重算摘要的篡改及送达丢失都拒绝。A4e-1 自检摘要和旧协议资格不能替代新资格。

## 4 Claude 过程证据与修复预算

复用冻结 CLI、OS 隔离、四字段 record_rule、钩子与原生事件采集。新 runner 显式使用所选套件、broker 和 verifier，不替换旧任务查找函数。运行前固定候选 revision/contentHash/exportHash、CLI/模型/工具、资格、预算、suiteDigest；运行后重新核验。隐藏初始/最终验收只在 Agent 退出后执行。

保留每一次公开检查的完整快照，包括失败和相同快照的重复检查。每个 checkpoint 关联 checkIndex、snapshot、recordSha256、原生 startedEvent/completedEvent、完整检查与分组结果、该点 cyclesUsed 及 editEventRefs。字段缺失不能通过模糊模型叙述补齐。

`extension-process-1` 使用宿主可核验的计数定义：一次编辑批次由两次已送达公开检查之间出现的 Edit/Write 调用组成；下一个公开检查结束该修复复验轮次。多个连续编辑属于一个批次，没有编辑的重复检查不增加轮次。为保守计数，失败或无净变化的编辑尝试也计入批次；记录其结果，不据最终相同哈希推断从未编辑。任务基线必须在第一次编辑前取得。只允许冻结的 Read/Edit/Write 与固定 MCP 能力，额外执行能力、调用配对缺失、检查与编辑重叠或顺序无法建立均保留为过程不完整。

过程输出至少包含 version、complete、issues、maxCycles=2、cyclesUsed、budgetExceeded、unverifiedEdits、checkpoints。校验原生 session/toolCall 身份和事件顺序；每个编辑必须可归属到一个批次，最后检查后的编辑必须保留且不能按早期绿灯授予最终过程资格。轮次从工具事实重算，不采信 record_rule 中自报的数字。

预算超限与行为正确分别表达：最终补丁仍可独立通过，完整事实中的超限不能被包装成正确使用或计入覆盖。过程缺口则阻断自动 repair_candidate 资格，保留 incomplete。完整失败记 failed，不删除运行。单次会话不能借用另一会话的检查或编辑。

## 5 schema 3 与人工分支约束

schema 3 / collectorVersion=extension-review-1 保留旧共同字段及 initialVerification/publicVerification/outcome/batchId/taskDigest，新增 suiteId、checkMapping、processVerification。taskDigest 对应 suiteDigest。collector 回读固定批次、资格、真实原生上下文、导出、所有公开快照及送达、初始/最终凭条，并调用同一纯过程重算函数。不能把报告中的 complete、轮次和分组状态当作证据。

processVerification 包含上述过程结果及每个 checkpoint 的引用；初始/最终 checks 使用有组名的稳定 ID，映射必须与套件完全一致。事件投影保持脱敏；中间快照和工件路径保存在宿主，页面只显示身份、状态、引用及已脱敏事件。普通 API 不提供任意上传 assessment 的入口。

outcome 暂限 repair_candidate/failed/incomplete。确认修复要求初始失败、最终公开及隐藏通过、行为变化、采集/加载/凭条/公开执行/送达/过程完整；确认修复不自动授予正确使用。人工覆盖仍要求 in_scope、correct、无严重违规、无预算超限，以及条件观察、真实动作、该规则声明和对应检查。每条分支必须引用本次事件和预登记目标，旧 schema 2 不能为新任务承载事实。

C1 仅预登记 confirm_contract#3、repair_scoped_reads#0、verify_scoped_reads#0、repair_dream_update#0、verify_dream_update#0。逐分支约束如下：

| 分支 | 必须有的过程事实 |
| --- | --- |
| confirm_contract#3 | 编辑前的真实基线显示 scoped 缺陷，明确合同与声明后才开始编辑 |
| repair_scoped_reads#0 | 初始 scoped 失败，实际编辑后某 checkpoint 六项 scoped 全通过，累计轮次不超过二 |
| verify_scoped_reads#0 | 同一 run 的中间 checkpoint：scoped 全通过、Dream 仍有业务失败、cyclesUsed=1、仍有一轮预算；条件引用该送达事件，之后有真实 Dream 编辑与规则转换证据，最后两组全部通过 |
| repair_dream_update#0 | Dream 缺陷先被真实观察，尚有预算时编辑，之后五项 Dream 全通过且未破坏 scoped |
| verify_dream_update#0 | 最终两组全部通过、结果完整送达、明确成功退出且后面没有未复验编辑 |

检查引用按固定映射要求相应组的完整验收集；初始缺陷和中间条件由 checkpoint 引用证明，不拿最终绿灯倒推出中间状态。若 Agent 一次修好两类问题，可确认最终修复及有充分证据的其他分支，但 verify_scoped_reads#0 保持缺失；不要求重跑以制造该路径。

实际登记为 Validation 和 schema 3 assessment 后仍无覆盖。沿用固定人工包、序号/来源水位/导出绑定和追加裁决事务；只有用户确认具体判断后提交。新的组合场景明确派生于两项核心探针，newIndependentSamples=0，不扩大利益实验样本。

## 6 实施工作包与验收

1. **P1 协议底座**：新 verifier/broker/qualification；实际十二矩阵、原 MCP 客户端兼容、异常取消/资源限制和篡改反例通过。
2. **P2 运行与过程**：新 runner/process/batch；固定加载、初始/最终验收、中间快照、原生送达、预算重算、取消与禁止覆写；合成过程必须区分真实两阶段、一步修完、超预算、改后撤回和结果缺失。
3. **P3 事实与治理**：新 collector/governance/importer，repository schema 3 分派；独立图测试验证幂等、完整事实门槛、跨运行拼接/伪轮次/错误分组/无中间状态拒绝，以及旧 schema 1/2 原判断不变。
4. **P4 页面与真实联调**：前端明确显示中间点、分组检查、轮次和覆盖缺口；真实 C1 none 校准后 fixed-skill 一次执行；导入、回读和页面展示；人工裁决最后单独确认。

新增代码先有失败测试，使用现有虚拟环境，不改依赖。各工作包中文提交；不推送或合并。服务由终端启动，浏览器展示前端验收。实际运行前先固定提交、资格和计划，运行失败保留原目录；不能开发中随意改动执行器再继续同一冻结批次。

完成条件：新协议真实资格通过；两次固定会话及全部结果保留；schema 3 实际入图与浏览器可审阅；旧 74 个冻结文件、52 份 P4 记录、原三次核心运行及四条人工裁决仍可回读（治理 repository 的新增分派单独记录，不改变旧 producer）。验收通过不等于 22 分支全覆盖或可发布；后续场景、42-B 和 P5 保留各自门槛。
