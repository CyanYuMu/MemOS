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
