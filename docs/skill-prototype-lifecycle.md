# 42-B 生命周期接口与实现合同

本文件是 [交付合同](skill-prototype-delivery.md) 的 D2 子合同，先于实现冻结。新增实现位于 `skill_lifecycle`；不修改旧实际执行协议或其摘要绑定文件。

## 1 主体与观察

宿主为固定候选绑定来源主体和工具主体。来源主体逐项绑定 Memory 服务身份、cube、Memory ID、来源 snapshot 和正文/metadata 摘要；公开 API 仅返回摘要和 subjectId。工具主体绑定固定 CLI 文件/版本、Read/Edit/Write、公开检查/规则记录的接口与实现摘要、Python 环境和本机只读能力探针结果。CLI 探针只执行固定 `--version`，不接受浏览器提供的路径或命令。

每次扫描实际 GET 来源及执行只读工具探针，分别形成 healthy、changed、unavailable、invalid；原异常恢复至预期摘要时形成 restored。连接失败不能称作变化；错误身份/所有者、缺字段或无效 JSON 为 invalid。观察包含检测器版本、预期/实际摘要、前一观察、时间和安全错误码，内容不可变。

相同结果不追加无限记录。异常根因在首次异常时建立，恢复观察只标记 restored_pending，不能自动清除发布阻断。每个异常起源按固定 REQUIRES 反向传播，逐个保留根因和完整路径；RELATED_TO 不传播。多根因独立清除。扫描/状态/发布及恢复事务均以项目锁串行化，改变受影响版本 sequence，旧包失效。

## 2 公开 API

`KEY={projectId,skillId,revision}`；`MUT=KEY+{expectedSequence,operationId,reason}`。全部新增命令要求 manage，actor 来自既有会话。以下字段为完整正文，不能夹带任意路径、命令或观察结果：

| 命令 | 正文 |
| --- | --- |
| impact_scan | MUT |
| impact_prepare | KEY |
| impact_review | MUT + packageId, packageHash, decision, rootIds, validationIds, replacementRevision |
| challenge_resolve | MUT + packageId, packageHash, challengeId, decision, evidenceIds, replacementRevision |

impact_review.decision 为 restore / retain / supersede；challenge_resolve.decision 为 addressed / upheld / superseded。rootIds、validationIds、evidenceIds 是无重复摘要数组；replacementRevision 为摘要或 null。restore/retain 时 replacementRevision 必须 null，supersede/superseded 时必须给同 Skill 新版本。所有 decision/reason 作为独立追加记录保存。

所有 subjectId、rootId、challengeId、evidenceId 和 watermark 均为 SHA256；sourceWatermark 是摘要数组。dependencyReleases 是 `{skillId,revision,releaseId}[]`。restore 的 validationIds 必须非空，rootIds 可空以恢复手工暂停；retain 两数组可空；supersede 两数组必须空。挑战 addressed 要求 evidenceIds 非空，upheld 可空且 replacementRevision=null，superseded 可空并由服务器绑定新发布。数组最长 200 项，不允许重复。

impact_prepare 返回：`{kind:'lifecycle',projectId,skillId,revision,sequence,contentHash,exportHash,sourceWatermark,lifecycleWatermark,activeRootIds,currentChallengeIds,evidenceIds,dependencyReleases,actor,preparedAt,packageId,packageHash}`。包只冻结当前事实，不授予恢复或发布资格。读取来源和探针失败时仍可准备诊断/保留包；恢复执行必须当前事实健康。

已启用生命周期的 review_prepare 包另增 lifecycleWatermark、dependencyReleases；旧未启用包保留原结构。adjudication_prepare 保持既有结构，扫描改变 sequence 使旧包失效。恢复可选 validationIds 从详情的 validations 与固定包 evidenceIds 交集中选择。

impact_scan 返回 `{sequence,changed,lifecycle}`；其他变更返回 `{state,sequence,lifecycle}`。详情新增 lifecycle：

```text
{schemaVersion:1, enabled:boolean, watermark:sha256,
 subjects:[{subjectId,kind:'memory'|'tool',expectedDigest,observedDigest:sha256|null,
   status:'healthy'|'changed'|'unavailable'|'invalid'|'restored',observationId:sha256|null}],
 roots:[{rootId,originSkillId,originRevision,subjectId,kind:'memory'|'tool',
   status:'active'|'restored_pending',latestObservationId,paths:string[][],reason}],
 observations:[{observationId,subjectId,kind,expectedDigest,observedDigest,status,
   previousObservationId:sha256|null,detectorVersion,reason,observedAt}],
 decisions:[{decisionId,action,actor,reason,decidedAt,evidenceIds:string[]}],
 dependencies:[{skillId,revision,releaseId:sha256|null,state,allowed:boolean}],
 gate:{status:'PASS'|'BLOCK',reasons:string[]}}
```

无生命周期绑定的旧合成测试数据仍可读取，enabled=false；真实服务启动为已登记候选绑定宿主基线。已有基线不可因重启换成新的 CLI 身份。公开主体和观察无连接凭据、文件系统位置或 Memory 正文。包和每次写操作都绑定内容、导出、水位和当前 sequence；冲突不自动重试。

## 3 恢复、挑战与替代

restore 只处理 rootIds 指定且当前已恢复的根因，要求 validationIds 为该固定候选/导出的新可信实际验证、不是失败/unknown，并且不是该根因发生时已有的验证。对应记录必须已完成人工/受托裁决且具备独立资格。全部根因清除后准入才可能恢复；手工 paused 版本也须当前包及新的验证，withdrawn 不可恢复。失效期间仍允许隔离验证，但不会返回正常加载许可。

挑战 addressed 必须引用当前包可见的验证/事实/裁决 ID；superseded 必须引用同 Skill 已发布且当前健康的新 revision，旧挑战保留正文并追加处置；upheld 保留开放阻断。不自动把旧 unknown 改为 passed。无需继续发布的旧版本可保留缺口或撤回，不能把“由新版替代”称为旧规则已证实。

supersede 仅在新 revision 已真实发布、来源/工具/依赖可用时创建版本替代关系，绑定新 releaseId；不撤回旧版本、不改变依赖目标、不授予旧版新证据。真实 SUPERSEDES 校验由管理层扩展，旧 graph producer 字节不变。

## 4 依赖与准入

声明中的固定 REQUIRES 必须在同一项目存在且无循环。发布和 admission 计算依赖闭包，要求每个依赖已发布、未撤回/暂停、无开放挑战/影响且资格有效；发布包与 Release 保存精确 dependencyReleases。依赖发布变化后不能自动绑定新的 release。反向传播包括观察根因及手工暂停/撤回状态；依赖恢复不会清除其他根因。

实际服务在评测/审阅/发布/normal 加载/任务结束前扫描当前版本及依赖闭包。每次扫描结果在事务内比对起始 sequence，防止网络观察期间并发发布使用旧包。依赖图变更、来源/工具变化或新决定使相关包失效。发布与挑战/失效竞争最多一方按旧 sequence 成功。

## 5 验收

RED 先覆盖模块缺失和旧 readiness 拒绝依赖；GREEN 包含真实 Neo4j 追加/幂等/并发、来源 changed/unavailable/invalid/restored、工具同版本字节变化、多根因分别解除、传播路径及 RELATED_TO 不传播、循环/缺端点/跨项目、陈旧包与恢复缺新验证、撤回不复活、替代不重绑。服务/API 测试验证真实探针只由宿主调用，权限/严格字段/安全错误与旧记录兼容。实际项目的扫描与恢复演示由主代理在服务切换后执行，测试只使用隔离项目。

## 6 已实现的宿主接线与恢复证据

配置结构不新增凭据或可由浏览器控制的探针。`build_service` 首次为真实候选调用 `subjects_for_packet(packet, cli_path)` 和 `bind_lifecycle`，以后重启复用原绑定。已登记候选在来源暂不可用时仍能启动治理；首次导入仍要求原有完整来源验证。服务的 `Project.observe` 使用 `HostObserver(ProductMemoryClient)`；纯合成旧测试可不配置 observer。

宿主扫描入口为 `scan_lifecycle(skill_id, revision, observer=..., actor=..., reason=..., operation_id=..., expected_sequence=None)`。它先读取依赖闭包全部 sequence，再在锁外执行去重的真实探针，最后在单个项目事务中比对所有 sequence 并追加事实。相同 operation 的重放返回原结果，不重新读取外部状态；新的扫描必须使用新 operation。观察使用独立 3 秒 HTTP 超时、最多 4 个并行探针和整闭包 10 秒预算，超预算记 unavailable 而非 changed；原 bridge 验证保留 20 秒超时。已阻断的 normal 准入直接拒绝，不再进行第二次慢来源读取。`lifecycle_subjects` 仅供宿主读取私有定位信息，HTTP 无对应入口。

恢复还绑定验证发生时的观察上下文：新增验证在管理状态记录当前依赖闭包 observation IDs 及是否均已恢复，原 validation/report 字节不变。restore 要求这些 ID 仍为当前、验证是在根因出现后新采集、通过当前可信独立裁决且实际任务验收成立。恢复前运行或缺少此上下文的历史验证不能作为恢复凭证。手工暂停也要新增验证；撤回不可恢复。

固定包的 `dependencyReleases` 仅列出已存在的非空 releaseId。未发布依赖仍在详情 `lifecycle.dependencies` 表示为 `releaseId:null, allowed:false` 并阻断发布，因而仍可准备严格符合 schema 的诊断包。真实发布不允许依赖管理测试 release；SUPERSEDES 只能由受审阅事务创建，不能通过通用图导入冒充授权。

新 `release-` 任务通过 `skill_release.service_runtime` 的显式注册表进入服务；旧任务入口保持原协议。正常运行与隔离运行均在执行前后调用服务准入，正常运行不产生独立分支覆盖资格。公开任务列表仅显示服务注册的修复、健康任务，不枚举独立评测的隐藏条件。

验证采用独立 Neo4j 项目，测试记录明确是事务和协议夹具，不计真实 Skill 成效。P5 可在私有 Claude 可执行文件副本上模拟不可用，再恢复原文件；真实 Memory 不可用演示仅停启本实验服务。内容变化、身份异常和同版本工具字节变化使用隔离探针验证，不能据此改写旧来源快照或声称生产来源内容已变化。

## 7 恢复新鲜度补充合同（执行票据，修复前先冻结）

仅在导入 validation 时保存当前观察不足以证明新执行：根因发生前完成、但一直未导入的旧 batch 可以在恢复后首次导入。第 6 节的导入时上下文保留为诊断信息，不再独立授予恢复资格。

增加宿主专用执行票据，无浏览器写入口：

- `begin_execution(skill_id, revision, *, batch_id, unit_id, case_id, group, mode, content_hash, export_hash, actor, operation_id)`：在真实执行前，已实际扫描来源/工具后调用。mode 为 independent_validation / normal；group 为 none / skill。图事务检查固定候选/导出、主体观察均健康，保存不可变开始记录及单调 startedSequence。由服务器产生随机量和 runId、attemptId，不接受客户端指定旧 runId 后补签。runId 为服务器生成的 64 位随机十六进制（保持底层 80 字符限制），attemptId 为服务器生成的 32 位随机十六进制；batchId + unitId 对固定候选只允许一个票据。返回 `{ticketId,runId,attemptId,startedSequence,observationIds,contentHash,exportHash,batchId,unitId,caseId,mode,group}`。相同 operation 的回读不构成再次启动 CLI 的授权，运行器仍要求全新输出目录。
- `finish_execution(ticket_id, *, report_sha256, events_sha256, initial_receipt_sha256, receipt_sha256, actor, operation_id)`：宿主真实运行完成、原生证据写盘且重新扫描后调用。图事务关联原票据，保存不可变完成记录，绑定 report/events/初始及最终 receipt 四个摘要、finishedSequence 和结束观察。开始与结束观察不一致或不健康时记录 invalid，不能将其当作恢复凭证。完成记录独立保存，避免将包含自身摘要的 report 写回形成循环依赖。

新 runner 在启动 Claude 前取得票据，并把服务器给出的 runId/attemptId 用于所有原生公共事件、报告与验收回执。新 collector 仍验证完整批次和原生证据，其 schema 6 assessment 已有 batchId、runId、attemptId、reportSha256、eventsSha256、initialVerification.receiptSha256、verification.receiptSha256，恢复事务用这些字段逐项匹配图内完成票据。不能只把可编辑的文件时间或客户端开始时间作为顺序依据。

根因新增 openedSequence，暂停使用追加事件的 sequence。restore 要求：当前可信独立资格对应 skill 组的 independent_validation 票据，startedSequence 严格晚于所清除根因的 openedSequence（或手工暂停事件）；开始、结束与当前观察 ID 一致且健康；完成摘要与当前 assessment 逐项相等；任务本身验收成立。根因前执行后延迟导入、根因前签发后执行、票据缺失/未完成、摘要不匹配、观察变化、同 batch/unit 重复签发均不得恢复。旧无票据记录保留全部回读、裁决与历史状态，但不能为其补发恢复资格。

回调责任：`skill_lifecycle` 负责图票据、单调顺序、实际观察与恢复门槛；`skill_release` 负责仅在新真实运行的 CLI 前/后调用，并验证票据 run/attempt 与原生链一致。服务正常运行沿用既有 before/after admission，执行票据不能绕过发布准入。真实 batch 仅在这项修订测试完成并冻结后启动。
