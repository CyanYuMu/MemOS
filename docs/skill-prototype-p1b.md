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

P1b 支持正文与来源可完整对应的短摘要。客户端处理实际返回的 ID 列表，不假定接口永远只返回一个 ID；但这不代表已支持所有后端分块格式。当前 MultiModalStruct 的长文本分块保留全文 `sources`，而每个 Memory 正文只包含部分文本；首版不能为这种形态证明完整对应关系，因此拒绝进入来源池。测试中的可核对多片来源属于契约测试，不冒充真实长文本分块验证。后续需基于批次、片段顺序和完整性信息单独设计分块协议。

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

以上是开发前冻结的验收标准；实际执行结果记录在后文，未执行实验不填充通过记录。

## 7 分支与提交

分支 `codex/skill-prototype-p1b`，基于 `c8e1a798`。先文档，再证据/采集、Memory 桥接、集成与结果；每条提交说明使用中文，显式暂存对应文件，不改写旧历史。既有未跟踪 `bin/` 不属于本轮。

## 8 使用方法

使用已经配置好的虚拟环境。显式执行 `.venv/bin/python`，避免测试启动器隐式同步依赖。Claude 的凭据通过受限环境变量传入，不通过命令参数、证据包或 Git 保存。此入口不读取和改写用户的 Claude 设置。

```bash
skill_artifacts="$HOME/.local/share/memos-skill-experiments"
mkdir -p "$skill_artifacts"
skill_source_root=$(mktemp -d "$skill_artifacts/source-XXXXXX")
PYTHONPATH=src .venv/bin/python -m memos.skill_experience collect-source \
  source-dream-update --output "$skill_source_root/dream" \
  --cli-path /path/to/claude --model FIXED_MODEL \
  --timeout 300 --max-turns 20 --max-budget 2
```

以新的输出目录运行 `source-scoped-update`。已有目录不会覆盖；失败后的重试也是新 Attempt。`collection.json` 指向前检、Agent 原始报告和 EvidenceRevision；原始报告仍标记 P1a 的 adapter validation 用途，来源资格由 P1b 的直接执行链单独判断。

```bash
PYTHONPATH=src .venv/bin/python -m memos.skill_experience verify-evidence \
  "$skill_source_root/dream/evidence/evidence.json"
```

MemOS 地址通过 `MEMOS_SKILL_API_URL` 配置，需要令牌时使用 `MEMOS_SKILL_API_TOKEN`；不要把私有配置写进以下命令或项目文档。指定专属来源 cube 和 owner，所有重试使用同一 ledger。

```bash
PYTHONPATH=src .venv/bin/python -m memos.skill_experience ingest \
  "$skill_source_root/dream/evidence/evidence.json" \
  --ledger "$skill_source_root/delivery.sqlite" \
  --snapshots "$skill_source_root/memory-snapshots" \
  --cube skill-source-experiment --user skill-experiment-owner

PYTHONPATH=src .venv/bin/python -m memos.skill_experience list-sources \
  --ledger "$skill_source_root/delivery.sqlite" \
  --snapshots "$skill_source_root/memory-snapshots" \
  --cube skill-source-experiment --user skill-experiment-owner
```

`refresh /path/to/memory-snapshot.json` 使用相同 ledger、snapshots、cube、user 参数，重新观察实际 Memory。`ingest` 的 `pending_reconcile` 需要再次执行同一命令核对远端，不能通过换账本“重试”；没有确切回读时仍保持未决。

采集命令退出码 0 表示合格成功修复，1 表示失败或缺证据，2 表示输入/操作错误；完整失败经验也返回 1，但可作为有限失败证据保存。写入返回 0 需要 `completed`，刷新返回 0 需要 `unchanged`。`verify-evidence` 的成功只表示证据一致，须同时查看 qualification，不能把 excluded 或 needs_evidence 当作来源。既有包初始化可能先输出诊断日志，结果以最后一行 JSON 及工件文件为准。

工件引用目前使用本地主机绝对 URI；移动目录、改变验证器/依赖或改写已封存文件会使重验拒绝。原始文件应整体保留，不能只保存 evidence.json。跨主机工件仓库和历史验证器兼容属于后续工程工作。

## 9 真实集成发现的适配修正

首次真实采集中，多个最终补丁已通过独立验收，但 Claude 仍尝试调用被禁用的 Bash 或向允许目录之外写入临时测试。CLI/OS 拒绝了这些操作，其早期拒绝路径不产生完整 hook 配对；按现有门禁，记录保持 needs_evidence。

排查发现启动提示仅要求修复任务、保持两个保护文件、调用固定检查，没有明确表达工具和文件边界。本轮补齐启动提示：只能使用本次明确开放的工具、只修改注册允许路径、不得创建临时测试或请求任意程序执行，额外正确性由宿主独立验收。此修正不改变 oracle、不放宽权限、也不绕过缺失记录门禁。补充实际命令生成测试后重新采集两个来源任务。

适配器内容摘要会随修正变化；旧报告和 Evidence 保留原文、原资格与当时的环境，当前实现拒绝把旧版本证据直接纳入新的来源池。后续实际结果须区分修正前后的运行，不把历史运行重新标成新版本通过。

## 10 实施与验收结果

2026-11-08，P1b 的 D01–D10 已完成本地验收。实际使用 Python 3.12.13、Claude Code 2.1.296、显式模型 `claude-opus-5-5`，每次配置 300 秒、20 turns、2 USD 预算上限；原生报告的轮次与费用保留原值，不将费用估计当作账单。实际服务为 MemOS 1.0.1 / contract 2、Neo4j 5.26.6、Qdrant 1.15.3。

### 10.1 自动测试与审查

```bash
.venv/bin/python -m pytest tests/skill_evaluation tests/skill_adapter \
  tests/skill_experience tests/api/test_public_no_skill.py \
  tests/dream/test_persistence.py tests/api/test_update_memory.py -q
```

最终结果：**519 passed in 81.63s**。其中 P1b 新增 93 项，启动提示修正新增 3 项，其余覆盖 P0/P1a 与相关 API 回归。Ruff、格式、每次提交的适用 pre-commit 检查通过，独立代码审查为 APPROVE。自动测试中的原生事件和 HTTP 故障夹具明确标注为合成数据；以下 Claude 与服务结果另行实际执行，不由 mock 测试推断。

### 10.2 当前适配器下的真实来源

| 任务尝试 | 最终独立验收 | 资格 | Agent 耗时 | 原生报告费用 USD |
| --- | --- | --- | --- | --- |
| S1 第一次 | failed：Fake 新增 `*args`，不符合严格签名合同 | qualified_failure | 103.962 秒 | 0.2456872 |
| S1 新 Attempt | passed：5 项检查通过 | qualified_success | 149.523 秒 | 0.2650864 |
| S2 | passed：6 项检查通过 | qualified_success | 215.944 秒 | 0.4396158 |

三次均先验证健康基线通过、初始故障按预期失败，再无 Skill 启动 Claude。最终候选、公开轨迹、回执、run/attempt 与 EvidenceRevision 重新绑定核查。S1 成功修复仅改 `caller.py`，S2 成功修复改 `fake_graph.py`；健康调用、错误调用与租户边界均由固定独立检查验收。失败尝试不冒充成功，根因归因仍由后续人工审阅判断。

修正提示前另有 **6 次**真实尝试：最终候选都通过，但其中 4 次因被拒绝的 Bash 或外部临时文件请求而缺少完整配对记录，资格为 needs_evidence；另外 2 次在当时版本合格。所有 9 次运行完整保留。旧版本记录不会通过当前实现的摘要核查，因此不纳入当前来源池；这不是丢弃失败样本后声称成功率提升。重复尝试仍只有 **2 个独立 case/lineage**，本轮未做 Skill 收益或统计效果实验。

### 10.3 真实 Memory 链路与故障验证

- 当前三个来源记录均通过真实 `/product/add` 写入，按实际 ID 和 cube 回读，正文、sources、证据字段匹配，`vector_sync=success`。可用来源池为 **2 条成功经验 + 1 条完整失败经验，覆盖 2 个独立案例**。
- S1 重复 ingest 仅回读，Memory ID 不变。S2 在真实服务成功写入后人为丢弃响应，先进入 pending_reconcile，再由新客户端精确查询、逐 ID 回读恢复 completed；该操作实际只 POST 一次。
- 错误 cube 读取返回空数据。仅受理、错归属、部分可见、空读回等边界由自动测试补充，均不能伪造可用快照。
- 独立 namespace 的同案例探针经真实 update API 修改元数据后返回 changed，旧快照字节保持不变，该探针不再出现在可用来源列表中。探针和重复 Memory 均不增加案例数。
- 提示修正前，实验 API 随启动父会话退出，造成一次真实投递结果未知。服务已改用独立后台 supervisor 并恢复原端口；精确查询仍无 ID 时，旧操作持续 pending_reconcile，未删除账本或重发。随后通过新的实际执行产生新 Evidence，而非改写旧身份。该保守策略的可用性代价已实际观察到。
- 当前运行工件、账本和快照保存在仓库外的持久目录；迁移保留原操作键、状态和全部快照字节，旧路径保留别名。历史临时运行另行归档；旧待决操作、旧版本写入和变化探针均保留，当前导出重新验证资格后排除它们。

验收对应：D01–D04 由真实采集及证据篡改/用途隔离测试覆盖；D05 由当前三条实际入库回读覆盖；D06–D08 由真实故障探针与自动测试共同覆盖；D09–D10 由最终 519 项回归、代码审查、注释、使用说明及提交检查覆盖。没有修改生产 API 默认行为，也没有清空或重建已有数据库。

### 10.4 交付边界与下一阶段

当前可运行的增量是：**Claude 执行受控任务 → 保存完整经验与证据 → MemOS 实际写入回读 → 提供可追溯来源输入**。Neo4j 此时承载现有 Memory 数据，尚未创建 P2 的 Skill Graph。

仍需保留这些限制：macOS 隔离、受信本地主机、短摘要回读、同一账本的幂等边界、手动触发刷新、历史实现/环境变化后重新验证。被拒绝操作的早期路径可能没有 hook，当前策略保守阻止准入；明确提示减少误用，但不保证所有 Agent 尝试合格。持久工件目录不可随意移动，连接地址变化也不能当作无风险重试方式。

下一阶段 P2 将消费真正回读的 MemorySnapshot，结合成功和失败证据生成有限适用范围的候选 Skill，并将每条规则、来源 Memory/Evidence 和 Skill 关系写入独立 Neo4j Skill Graph。人工审批发布、独立任务效果比较、版本替代与失效传播继续属于 P3–P5；本轮没有提前宣称 issue 全链路完成。
