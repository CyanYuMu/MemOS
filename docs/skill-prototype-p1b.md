# P1b：来源案例证据与 MemOS 写入回读

日期：2026-11-08。依据：[冻结设计第 4–6 节](skill-extraction-design.md)、[总计划](skill-prototype-development.md)、[P1a](skill-prototype-p1a.md)。本文件先固定范围和验收，再开始实现。

## 1 目标与范围

本轮接通 `真实来源任务执行 → 完整证据 → MemOS 保存 → 实际回读 MemorySnapshot`。复用 P1a 的 Claude Code 适配器和 P0 的受控任务、快照与外部验收，重点补齐 issue 的“收集和保存经验”。

- S1/S2 使用 `source-dream-update`、`source-scoped-update`，无 Skill 运行；任务身份来自固定注册表。
- 执行前验证健康基线通过、初始故障按预期失败；最终修复由原有独立 oracle 验收。初始故障回执的候选摘要必须与实际 Agent 初始快照相同。
- 形成不可变 EvidenceRevision、代码变化记录、资格与缺口；保留失败和重试，不覆盖旧 Attempt，不修改 P0/P1a 原始报告的资格字段。
- 使用真实 `/product/add` 同步 fast 路径保存可检索案例摘要和显式证据引用，再按实际返回 ID 调用 `/product/get_memory/{id}`，始终指定 cube 与 `include_sources=true`。
- 通过持久化投递账本管理幂等、响应丢失与进程恢复；实际读回正文、来源和元数据后才创建 MemorySnapshot。
- 增加说明边界和失败原因的代码注释。使用独立分支与中文提交，凭据、端点、完整轨迹留在仓库外。

本轮不实现自动提炼、Skill Graph、审批发布、对照收益实验或更新传播，这些仍按 P2–P5 推进。同步 fast 写入保留案例摘要，并不等于已提炼出 Skill；提炼器后续必须消费真正读回的 MemorySnapshot。

## 2 接入选择与已发现的限制

现有 product 写入会返回 `memory_id/cube_id/memory`，详情接口可返回完整来源。GeneralText 的 add/get 契约与 product 的租户路径并不相同，不能用直接写 Qdrant 替代本轮服务集成。

`task_id` 是处理关联 ID，不是完整幂等键；现有 two-stage 只有 coarse ID 稳定，最终写入仍可能生成新 ID。本轮使用单机 SQLite 投递账本与显式来源字段，不改写生产 API 的默认行为：

1. 唯一操作键绑定服务身份、cube、owner、EvidenceRevision；请求正文摘要固定。
2. 在网络发送前持久化 `sending`。同一账本内并发或重复请求不能再次 POST。
3. 成功响应保存实际 Memory ID；后续重试仅回读。
4. 超时、断连或进程中断后进入 `pending_reconcile`，只通过指定 cube 下的精确 operation/evidence 字段查询恢复，再逐 ID 读取。
5. 查询为空不证明之前未写入，不能盲目重新 POST；多条或不一致记录保持冲突/未决。不同账本或不合作的外部写入方不在本地幂等保证内。

这明确选择了“宁可保留未决，也不在提交状态不明时制造重复”的取舍，不声称跨 HTTP、数据库和工件存储实现 exactly-once。异步受理或只有 task_id 的响应不能产生 MemorySnapshot；P1b 首版不提交异步作业。

## 3 证据包契约

EvidenceRevision 使用规范化内容摘要作为修订身份，记录：

| 内容 | 来源与核查 |
| --- | --- |
| project/case/revision/lineage/split/run/attempt | 固定注册表、来源 provenance 与 Runner 上下文 |
| 任务合同 | 合同、允许修改路径、预期正反检查、完整任务资产摘要 |
| 初始与健康基线 | 可恢复快照及真实 guarded 验收回执；初始失败必须是预期行为失败 |
| 环境 | Python/依赖/平台、CLI/模型/工具/预算、隔离与实现摘要 |
| 公开事件 | JSONL 哈希、原生 session/工具 ID、顺序、双通道配对和缺口 |
| 最终候选与变化 | 可恢复完整快照、文件增加/删除/内容/属性变化；文本 diff 仅作辅助 |
| 最终验收 | case/run/attempt/候选身份、实际检查、状态、receipt/hash 与重新核查结果 |
| 来源、完整性与资格 | 受信本地采集器、采集时间、工件引用/哈希、资格理由、未知项 |

资格分开表达：`qualified_success`、`qualified_failure`、`needs_evidence`、`excluded`。成功必须有完整采集和最终绑定通过；失败只支持已观察到的失败事实，根因/可归因性未经审阅保持未知。初始失败缺失不能支持修复因果，错误/中断不自动当作可排除的基础设施失败。模型费用和用户反馈缺失只限制对应比较。

只有受信本地采集流程直接执行的 source 运行可进入来源池。dev/holdout、手写 Skill 探针、参考方案、自检与任意导入 JSON 都不能自动升级为来源经验。哈希证明记录一致性，不能认证第三方执行；首版信任控制面主机和工件目录，不提供外部 JSON 强制授资格入口。

Case/lineage 是独立案例身份；多次 Attempt、多个 Memory 或多个 EvidenceRevision 不增加独立案例数。P0 receipt 的 `qualified_agent_experience=false` 保留，P1b 的资格判断在新记录中给出。

## 4 MemoryBridge 与快照契约

桥接器写入简短、可检索、由已核查事实组成的案例摘要，包含目标、已观察故障、公开操作/文件变化、最终检查结果及未知项。完整工具轨迹与代码保存在受保护工件存储；Memory 存显式引用，不依赖 LLM 保留逐字段原始日志。

来源使用 `info` 中非保留字段，包含专属 namespace、operation_id、EvidenceRevision、case/lineage/run/attempt/split 和摘要哈希；读回必须逐项核验。精确来源信息丢失或正文为空时保持未决，不通过相似度或“最近一条”猜测关联。

MemorySnapshot 保存 backend/service identity、cube、实际 memory_id、观察时间、实际正文/来源/必要元数据、内容摘要、响应工件和 EvidenceRevision 引用。新观察形成新快照；读不到、服务错误、明确删除、内容变化分别记录。没有明确删除依据时只标记 unavailable。快照变化会使当前可用性检查报告 changed，不改写旧快照。

source 使用独立来源 cube/namespace；dev 与 holdout 不写入该入口。读取提炼输入必须再核对注册用途与 Evidence 资格，而不是相信 Memory 的普通标签。后续页面或模型不能通过改标签重标用途。

## 5 开发顺序与责任

| 工作包 | 主要内容 | 预计代码位置 |
| --- | --- | --- |
| P1b-0 | 启动并核查已有 Neo4j/Qdrant、真实 MemOS API 和模型/embedding 配置，使用专属实验数据 | 仓库外启动配置；记录版本与脱敏结果 |
| P1b-1 | 初始失败/健康基线前检、source 采集编排、保留所有尝试 | `src/memos/skill_experience/collection.py` |
| P1b-2 | EvidenceRevision、差异、资格、可信工件重验 | `evidence.py` 与对应测试 |
| P1b-3 | product HTTP 客户端、SQLite 投递账本、精确恢复、逐 ID 回读 | `client.py`、`bridge.py` 与对应测试 |
| P1b-4 | 命令入口、两次真实来源采集、实际入库回读与故障验证 | `__main__.py`、集成测试及本文件 |

先提交本文，再用有意义的失败测试实现契约。领域模块独立，原有 Memory/Skill API 默认行为保持兼容；若实际探测发现服务缺陷阻挡上述契约，先记录最小修复范围，再补测试和实现。只启动任务需要的服务，不清空或重建已有数据。

## 6 阶段验收

| 编号 | 必须满足的条件 |
| --- | --- |
| D01 | S1/S2 健康基线通过、初始故障按预期失败；Agent 的初始快照与失败证据绑定 |
| D02 | 两个无 Skill 的真实 Claude source 运行产生最终候选，并通过独立验收；失败重试完整保留 |
| D03 | Evidence 可恢复并重验全部关键引用；篡改、跨 run/session 拼接、删事件、改补丁等拒绝授资格 |
| D04 | dev/holdout/参考补丁/未知生产者不能进入来源池；重试与多 Memory 不增加独立案例 |
| D05 | 通过真实 product API 写入，返回实际 Memory ID，再按 cube/ID 回读正文、来源与证据引用；真实 Neo4j/Qdrant 服务参与 |
| D06 | 只有受理回执、task_id、空回读、来源丢失、错误 cube 或多义映射均不能形成可用快照 |
| D07 | 同一账本的重复/并发投递不重复 POST；响应丢失后精确恢复；不明状态保留 pending 而不盲目重试 |
| D08 | 实际 MemorySnapshot 可核查；正文/元数据变化形成新观察，旧快照不可冒充当前状态；读取失败不推断删除 |
| D09 | 新测试、P0/P1a 与相关 API 回归、Ruff 和 Git 提交检查通过；真实服务/Agent 验证与 mock 测试分别报告 |
| D10 | 关键边界有代码注释；使用说明、服务版本、真实验收和限制写入文档；凭据与私有部署细节不入仓库 |

当前仅冻结开发与验收标准；实施结果在完成后追加。没有用尚未执行的实验填充通过记录。

## 7 分支与提交

分支 `codex/skill-prototype-p1b`，基于 `c8e1a798`。先文档，再证据/采集、Memory 桥接、集成与结果；每条提交说明使用中文，显式暂存对应文件，不改写旧历史。既有未跟踪 `bin/` 不属于本轮。
