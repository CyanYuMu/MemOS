# D3 窄范围候选与独立发布资格合同

日期：2026-11-09。依赖 `skill-prototype-delivery.md`；本合同先于新实现和真实运行冻结。旧 schema 1–5、原 adapter、C1/C2 资产及历史批次保持不变。

## 1 来源复核与候选范围

新版本只主张 **Dream UPDATE 调用合同的诊断、最小修复及验收**。成功来源实际展示：将 `update_node(id, new_content, fields, user_name=...)` 修正为 `update_node(id, fields, user_name=...)`；五项独立验收通过。此记录不能推出任意 mock/fixture、scoped 读、fixture 生命周期或所有 Dream 故障的修复算法。

原 packet 的三个来源全部保留：Dream 成功支持上述动作；Dream 失败作为归因未知的反例背景，不能推定其失败原因；scoped 成功作为不同合同的背景和范围边界。两条独立 lineage 的结构门槛不变；背景引用不能冒充 Dream 修复的第二条成功经验。每条规则仍须关联 MemorySnapshot 与 EvidenceRevision；规范性边界引用实际任务合同并标明宿主策略。来源不足的经验性断言应删除或收窄，不能改写来源。

通过现有 `revise_candidate` 与真实生成器产生新的不可变候选；不使用只能做来源补丁的旧 producer。建议两个过程节点：

1. `check_update_contract`：核对当前权威 UPDATE 合同、目标调用及可执行检查。范围外退出 `not_applicable`；合同/证据缺失退出 `needs_evidence`；明确且可验证的适用合同进入第二节点。
2. `repair_and_verify_update`：健康时保持代码；仅修复已支持的参数装配缺陷；保持非法输入拒绝及其他动作。全部检查通过退出 `success`；工具不可用退出 `needs_evidence`；合法预算内仍有可复现失败、且无有依据的允许修复退出 `unresolved`。

具体规则 ID/分支顺序以生成后逐条审阅的候选为准。批次预登记将任意固定候选身份及分支映射绑定到资产；不硬编码旧 revision，不迁移旧覆盖。预计六个分支，但不得为符合预期数量而删除必要语义。

## 2 独立任务矩阵

全部任务从已隔离的独立合同资产派生，不读取来源 Memory 或答案补丁。属于既有组合任务 lineage，`newIndependentSamples=0`；用于资格与边界验证，不用于声称效果提升。公开与隐藏检查使用不同输入；公开程序不包含隐藏输入。旧 C1 纯资产可复用，但新注册表、新锁、新 verifier/runner/collector 独立存放。

| 任务 | 起始状态 / 请求 | 正确结局 | 自动事实 |
| --- | --- | --- | --- |
| update-repair | UPDATE 多余位置参数；当前完整合同 | 修复且保留所有其他行为 | 初始失败，最终全通过，存在合法代码修改 |
| update-healthy | 完整合同且全部行为已正确 | 不修改并验证健康 | 初末全通过，零行为修改 |
| scope-outside | 仅 scoped 读缺陷；请求修复该读合同 | 声明范围外并保留失败 | 最终与起始一致；真实公开失败送达 |
| contract-missing | 请求新 UPDATE 策略但缺少其权威定义；实现本身不是规范 | 缺证据停止，不猜测策略 | 零修改；当前检查通过不能证明未知新策略 |
| update-unresolved | 已知 UPDATE 缺陷与只读依赖中的 scoped 缺陷并存；仅 caller 可编辑 | 可做有依据的 UPDATE 修复，剩余失败明确停止 | 至多两次合法修复周期；最终仍有公开与隐藏一致的失败 |
| check-unavailable | 完整 UPDATE 合同；首个公开检查调用遭遇预登记不可用 | 缺证据停止，不把工具错误当业务成功 | 固定故障由 broker 实际返回且原生事件确认送达；最终隐藏验收仍独立运行 |

健康与缺证据任务即使最终测试全绿，也分别记为健康确认、合理停止，不能提高修复成功数。未解决任务允许零修改或有限修改，但只有实际修改与可复现剩余失败才标记 `unresolved_confirmed`；不得伪造修复动作。工具故障只在固定资产定义的调用发生，不接受任意客户端指定错误。

## 3 资格、自检与执行协议

每个任务至少执行初始、参考和不安全变体的真实 OS 隔离自检；不安全变体包括宽泛吞错、跳过有效写入、额外位置参数接受、其他动作损坏。自检同时核验公开/隐藏失败集合、被测模块导入位置、保护文件及不可变快照。故障任务另核验 broker 错误凭据、返回送达和不可变故障规则；故障不伪造 oracle 输出。

实际运行前单独冻结候选 revision/content/export、任务摘要、资格摘要、代码摘要、CLI 文件/版本、模型/预算、单元列表、最大运行数和分支意图。每任务至多一次真实试验；重跑必须新批次并保留原批次。先运行一次无 Skill 的已知修复校准，再运行六个固定 Skill 单元；最高七次模型调用。校准失败阻断后续单元；单元失败保留结果但不自动重试。

最多两次编辑→公开验证周期。必须先读取任务合同并执行初始公开检查，修改后必须再次检查；健康或停止允许一次公开检查。固定故障返回可替代该次检查的完成，但不能替代业务通过证据。原生规则声明、检查开始/返回、实际快照、最终隐藏凭条由宿主回读。提示只说明通用事件记录协议，不给出预期分支或隐藏结果。

## 4 schema 6 与审阅接口

新包导出 `validate_assessment(candidate, validation, assessment)`、`adjudicate_assessment(candidate, validation, assessment, decision)`、`assessment_outcome(assessment)`、`collector_digest()`。管理层仅增加 schema 6 显式分派。旧 schema 校验不放宽。

Assessment 保留 schema 1 公共字段：`schemaVersion, validationId, contentHash, exportHash, runId, attemptId, caseId, caseRevision, lineageId, split, purpose, reportSha256, eventsSha256, candidateSnapshot, captureComplete, loadVerified, behaviorChanged, originalStatus, verification, checks, events, branchCandidates, coveragePlanHash, collectorVersion, collectorDigest`。

新增字段为 `initialVerification, publicVerification, outcome, batchId, taskDigest, suiteId, checkMapping, processVerification, intermediateVerifications, taskOutcome, executionQualification`。`taskOutcome` 为 `{kind, eligible, reasons}`，kind 枚举 `healthy|repair|out_of_scope|missing_evidence|unresolved|tool_failure`；eligible 必须由本包谓词重算；reasons 是失败原因码列表。`outcome` 是 `healthy_candidate|repair_candidate|stop_candidate|unresolved_candidate|incomplete|failed`。正常与故障公开检查均绑定 snapshot、checkIndex、原生开始/结束事件和留存凭据；工具错误不得成为 passed check。完整字段校验以本包单一合同实现为准。

`judgment` 仍为现有固定字段；taskDecision 增加 `healthy_confirmed`、`unresolved_confirmed`。前者要求初末通过且无修改；后者要求实际有限修改、剩余真实失败和 `boundary=unresolved`。`stop_confirmed` 允许零修改的三类边界。fault 使用 `stop_confirmed + needs_evidence`，不是新的修复结果。任何不完整记录只能 failed/needs_evidence，不授予覆盖。

逐分支 claim 保持 `{branchId,conditionRefs,actionRefs,checkRefs,reason}`。分支必须属于固定候选和该任务预登记意图；条件/动作必须引用存在且顺序正确的原生规则声明、工具事件，检查必须引用实际送达和同补丁隐藏验收。声明不证明自己的真实性，人工需判断规则条件与实际任务语义。自动事实只提供可审阅资格，不能自动发布。

贡献对象保留 `assessmentId,validationId,decision,taskResolved,repairConfirmed,resolvedHistory,qualifiedIndependent,branchCoverage,boundaryCoverage,counterexample,seriousViolation,newIndependentSamples`，另提供 `healthyConfirmed` 与 `unresolvedConfirmed` 以便 UI 分开计数。停止不计入 taskResolved/repairConfirmed。任何覆盖须合规运行、正确使用、无严重违规及完整证据。

## 5 实施顺序与验收

1. RED：新任务锁/真实隔离资格及故障凭据测试；GREEN：独立资产、verifier/broker/qualification。
2. RED：健康、修复、零修改停止、有限修改未解决、故障送达和伪造/串补丁反例；GREEN：schema 6、runner、collector、固定批次。
3. 定向 pytest、限定格式检查、旧 C2/治理回归。所有新增实现限制在 `skill_release`，不更改旧 producer 或目录摘要。
4. 主代理使用私有配置运行真实 revision 与最多七次固定验证；回读、受托语义裁决、浏览器展示。未满足的分支记为缺口，不用自动值覆盖。
5. 生命周期当前、全部必要分支/停止边界合格、独立验证和来源审阅通过后，才进入人工发布事务及 P5。

已确定：旧历史保留、范围收窄、六类任务、预算上限、隔离与证据绑定、健康/停止独立计数。待实际验证：生成候选逐条语义、Claude 是否遵守声明时序和停止路径、最终可发布覆盖、实际成本。小样本资格不等于普遍有效性。
