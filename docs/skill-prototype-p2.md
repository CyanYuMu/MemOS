# P2：基于真实 Memory 的候选提炼与独立 Skill Graph

日期：2026-11-08。依据：[冻结设计](skill-extraction-design.md)第 6–8、14 节和[P1b 验收](skill-prototype-p1b.md)。分支 `codex/skill-prototype-p2`，从 `37a048ca` 创建。本文先冻结开发范围与验收，结果在实际执行后追加。

## 1 本轮目标与边界

接通 `固定来源计划 → MemOS 重新回读 → 证据核查与精确片段 → LLM 候选提炼 → 确定性检查 → 不可变候选工件 → 独立 Neo4j Skill Graph`。

当前输入为 P1b 的两个成功 Attempt 和一个完整失败 Attempt，只有两个独立 case/lineage。失败归因仍为 unknown，不能被模型升级为已证实根因。候选必须包含适用条件、诊断与操作步骤、分支、退出、约束、不适用场景及验收要求，每条规则绑定真正的 Memory 内容与原始证据。

本轮不实现普通使用授权、人工发布服务、Go/React 管理页面、holdout 效果实验或自动失效传播。结构和引用通过只产生未发布候选；语义蕴含、适用范围和反例裁决明确待人工审阅，不能把一次模型生成或图写入当作 Skill 有效。

## 2 关键选择与风险

1. **固定完整来源计划**：由宿主提供 project、明确的 Memory scope、EvidenceRevision 和实际 Memory ID 集合。逐条调用 P1b 的真实回读与重验；任一计划输入失效即阻断本批次，不静默丢弃失败经验。`list_sources()` 的便利过滤不用于证明输入池完整。计划是显式选择集，不声称已经检索到所有可能反例。
2. **真实 Memory 是提炼输入**：模型收到实际回读正文和来源引用，以及经重验的合同、初始/最终检查、文件变化、环境与 unknown 片段。不能从本地日志先生成，再补挂 Memory。去重使用内容与 Evidence 身份，独立性按 case/lineage；观察时间变化不算新经验。
3. **模型只提出候选**：使用已安装的 OpenAI 兼容 SDK，新增独立、延迟加载、无工具的 JSON 生成入口，不改旧 LLM 的默认行为。显式模型/预算/超时、禁止自动重试和备用模型；只保存公开最终内容、请求/响应摘要、用量与模型标识，不保存私有推理或凭据。现有封装不透传 JSON 参数且可能记录完整响应，因此不直接复用其 generate 路径。
4. **不修改 P1b 的可信实现**：已有 `skill_adapter/*.py`、`skill_experience/evidence.py` 和依赖环境参与旧证据摘要。P2 新建领域模块，避免无意让真实来源全部失效；不运行依赖自动同步。
5. **独立 Neo4j 实例**：创建独立容器、端口、数据卷和凭据。不能仅在现有 Memory 图增加标签，也不假设 Community 多库隔离。连接时核对数据库身份，拒绝写入已登记的 Memory 数据库。采用普通唯一约束和应用层必填校验，不依赖 Enterprise 的存在性/类型约束。[Neo4j 约束说明](https://neo4j.com/docs/cypher-manual/current/schema/constraints/)
6. **保守证据语义**：事实、规范合同与假设分开。归因未知的失败观察只作背景；合同可以支持规范性规则，但不能伪装成实证收益。引用可解析不等于逻辑蕴含，所有语义审查仍 pending。

## 3 数据和处理契约

### 3.1 来源与提炼输入

来源计划记录 `schema_version/project_id/scope/sources`；scope 显式绑定服务身份、cube、owner、namespace，不从普通 Memory 标签推断授权。每项 source 固定 evidence_path、evidence_revision、memory_ids。缺失身份、重复身份冲突、非 source、证据过期、跨 scope、空正文或内容变化均拒绝。

采集器生成不可变输入包，包含实际 MemorySnapshot、EvidenceRevision、来源集合摘要与片段表。每个片段包含目标类型和身份、JSON Pointer、实际值摘要、来源身份与事实类别；定位仅作用于已核查文档，不允许模型通过 URI 任意读文件。模型只看到有界公开投影，不接触 holdout、oracle 源码、连接信息或凭据。

触发最低门槛为两个独立相关 lineage，至少一个成功；当前实际运行使用两个成功和一个失败。至少固定共同语言/机制与合同背景，无法判断相关性时不强行合并。模型输出须覆盖计划中每条输入的支持或限制，不能忽略失败输入。输出候选前再次回读来源，内容水位改变则本次生成拒绝封存，保留失败作业记录。

### 3.2 候选规范与检查

规范形式为 `procedure`，用途为 diagnose/repair/verify，标签为平面列表。规则包含稳定 rule_id、条件/观察判据、动作、预期结果、分支目标、依据类别与引用；退出集合至少包含 success、not_applicable、needs_evidence、unresolved。步骤引用必须存在且可到达退出，禁止悬空分支、无终止循环和动态命令插值。

每条规则至少关联一条实际 Memory 片段和一条原始证据片段。证据关系表达 supports、contradicts、precondition、background，包含不可变 link_id、locator、claim 与 producer。规范规则须有合同依据；假设必须标记待验证，失败归因未知不能作为成功支持或已归因反例。每条 link 自动检查定位与摘要，语义状态固定 pending。

Skill 身份由调用方指定，revision 由规范内容和固定输入/生成上下文的摘要确定。模型不能指定发布状态、审批人或普通使用资格。工件包含输入包、原始公开模型结果、规范候选 JSON、供审阅的 Markdown、结构检查报告和完整 manifest；先写完整工件，再写图引用。审阅正文不等于已导出给 Agent 使用的原生 Skill。

作业输出目录不覆盖，失败记录保留安全错误类别。重复图导入同一不可变工件幂等；不把重新调用模型当作网络重试，也不保证模型再次生成相同文本。生成用量与来源水位保留；P2 不把结构报告冒充独立任务 Validation。

### 3.3 Skill Graph

保留冻结设计的九类节点：Skill、SkillRevision、Rule、MemorySnapshot、CaseRevision、Run、EvidenceRevision、Validation、Release。实际候选使用前八类；Validation 明确为结构/引用检查。Release 只在隔离的合成管理测试中演示，标记为非授权记录，不伪造真实发布。

来源边为 HAS_REVISION、HAS_RULE、DERIVED_FROM、REFERENCES、EVIDENCE_LINK、FROM_RUN、OF_CASE；结构检查用 EVALUATES/USES_RUN。RELATED_TO 对称、仅导航；REQUIRES 指向精确版本且无环；SUPERSEDES 限同 Skill、指定范围的替代，不表示撤回。

本轮真实候选没有凭空添加必需 Skill。REQUIRES/审批替代的完整例子使用明确标记的合成管理项目，不能混入经验统计或普通使用入口。真实依赖声明必须与不可变 revision 一致；P3 再接人工授权事务。

图节点/边使用固定类型白名单、带 project 的身份、规范 payload 和摘要；唯一约束加事务内冲突检查，禁止同名覆盖。项目写入先竞争同一图范围锁，再验证端点、拓扑和不可变性，一次事务提交，避免并发检测各自通过后形成环。遍历有界并核对所有中间节点，不能跨项目绕行。支持读取候选、逐规则追溯、从 Memory 反向定位受影响版本/规则及拓扑导航。

图写入失败不删除已封存工件；可重试同一导入。图是证据关系记录，不自动证明语义正确或当前可用；跨 Memory/文件/Neo4j 不做分布式事务，保存最近核查水位，导入前再次核对。

## 4 开发顺序与工作包

| 包 | 范围 | 代码位置 |
| --- | --- | --- |
| P2-0 | 本文、独立服务配置探测、输入和接口契约 | 本文及仓库外运行配置 |
| P2-1 | 来源计划、真实回读、片段定位、输入封存/水位检查 | `src/memos/skill_drafting/sources.py` |
| P2-2 | 候选 schema、规则与引用检查、不可变工件与审阅渲染 | `domain.py`、`artifacts.py` |
| P2-3 | 有界无工具模型调用、提炼提示、作业记录 | `generation.py`、`pipeline.py` |
| P2-4 | 独立 Neo4j 仓库、事务约束、追溯/反向查询 | `graph.py` |
| P2-5 | CLI、真实三来源提炼和图验证、结果与 P3 缺口 | `__main__.py`、测试和本文 |

先提交本文，再按有意义的失败测试实现；纯协议与图仓库可并行，实际提炼等待输入/候选协议稳定。旧 Memory API 和服务默认行为不变。不改用户未跟踪的 `bin/`，显式暂存文件，按功能作中文提交，不推送或合并。

## 5 验收矩阵

| 编号 | 必须满足 |
| --- | --- |
| E01 | 实际重新回读当前三个来源；内容、Evidence、scope 和用途匹配；仍只有两个独立 lineage |
| E02 | 真实模型消费实际 Memory 正文和证据片段，自动生成候选；保留输入、公开输出、模型/预算与摘要 |
| E03 | 每条规则引用可解析到实际 Memory 和 Evidence；悬空、篡改、未知来源、错误类型或失败经验滥用被拒绝 |
| E04 | 候选具有条件、步骤、分支、出口、约束与验收；未支持边界标记限制/待验证，不宣称发布或收益 |
| E05 | 来源不足/变化/读取失败不产生成功候选；重复 Memory、重试或生成结果不能增加独立案例；失败输入不静默丢失 |
| E06 | 独立 Neo4j 实际写入并读回候选、规则、Memory/Evidence/Run/Case及结构 Validation；原 Memory 数据保持可读 |
| E07 | 幂等导入、不可变冲突、断链、跨项目与错误数据库拒绝；循环依赖（含并发）不能提交半成品 |
| E08 | RELATED_TO、REQUIRES、SUPERSEDES 和九类节点以明确合成管理夹具验证，真实候选不伪装依赖/发布 |
| E09 | 可从规则追溯来源，也能从 Memory 反查版本/规则；服务重启后图数据保持，结果有界且不跨项目 |
| E10 | 新测试、相关 P0/P1 回归、Ruff、提交检查及独立审查通过；实际结果与合成测试分开报告 |
| E11 | 开发/使用文档、风险和 P3 缺口明确；凭据、私有端点、完整运行工件留在仓库外 |

当前仅冻结范围和验收，未填入尚未执行的通过结果。
