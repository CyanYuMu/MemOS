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

## 6 使用入口与环境契约

新增模块为 `src/memos/skill_drafting/`，不修改旧 Memory API、Claude 适配器和 P1b Evidence 的可信实现。

| 文件 | 责任 |
| --- | --- |
| `sources.py` | 核对固定计划和已登记的交付记录，真实回读全部来源，生成精确事实及水位 |
| `domain.py` | 来源、规则流程、证据资格和生成溯源检查，计算候选及规则引用身份 |
| `artifacts.py` | 原子封存候选、输入、公开生成记录、结构报告及审阅正文，回读时重建核验 |
| `generation.py` | 固定模型的单次无工具 JSON 请求，公开投影、输出与隐私边界 |
| `pipeline.py` | 采集、生成、检查、二次回读及封存；失败保留独立作业记录 |
| `graph.py` | 独立 Neo4j 的项目事务、不可变节点/边、拓扑约束和追溯查询 |
| `graph_bundle.py` | 将已核验工件映射成图；导入前重验活跃来源及原始文件内容 |
| `__main__.py` | 六个显式 CLI 命令，固定类别错误和 JSON 输出 |

先通过受信任的本地配置注入环境变量，不把实际值写入文档、命令行参数或 Git：

| 环境变量 | 用途 |
| --- | --- |
| `MEMOS_SKILL_API_URL`、可选 `MEMOS_SKILL_API_TOKEN` | P1b 使用的 MemOS 服务，服务身份必须与来源计划一致 |
| `MEMOS_SKILL_LLM_BASE_URL`、`MEMOS_SKILL_LLM_API_KEY`、`MEMOS_SKILL_LLM_MODEL` | 显式模型配置，三者必填，无备用模型 |
| `MEMOS_SKILL_GRAPH_URI`、`MEMOS_SKILL_GRAPH_USER`、`MEMOS_SKILL_GRAPH_PASSWORD`、`MEMOS_SKILL_GRAPH_DATABASE` | 独立 Skill Graph 连接，四者必填 |
| `MEMOS_SKILL_GRAPH_MEMORY_DB_ID` | 只读取得并登记的原 Memory 数据库身份；误连该库时拒绝任何 DDL/写入 |

在已准备好的项目环境中运行下列模板；尖括号表示自行替换的本地路径或身份，不是实际配置。

```text
.venv/bin/python -m memos.skill_drafting draft \
  --plan <source-plan.json> --output <new-job-directory> \
  --skill-id python-mock-fixture-contract-repair \
  --ledger <existing-delivery.sqlite> --snapshots <existing-snapshot-directory> \
  --timeout 180 --max-tokens 16000
.venv/bin/python -m memos.skill_drafting inspect --candidate <candidate-directory>
.venv/bin/python -m memos.skill_drafting graph-import \
  --candidate <candidate-directory> --project <project-id> \
  --ledger <existing-delivery.sqlite> --snapshots <existing-snapshot-directory>
.venv/bin/python -m memos.skill_drafting graph-inspect --project <project-id>
.venv/bin/python -m memos.skill_drafting trace \
  --project <project-id> --rule-key <skill-id@revision#rule-id>
.venv/bin/python -m memos.skill_drafting affected \
  --project <project-id> --memory-id <memory-id>
```

`draft` 使用全新输出目录；模型超时默认 90 秒，输出预算默认 6000，Memory 请求超时默认 30 秒。退出码 0 表示操作完成，1 表示生成失败且有作业记录，2 表示输入/操作失败；没有自动重试和发布命令。调用者应保留失败目录，调整配置后使用新目录。

失败模型调用仅保留固定公开诊断：请求/报告模型、结束标识、用量及其可用性、参数、提示/schema 摘要和耗时；不保存截断正文、原始异常或私有推理。完整但不合法的公开输出保留为失败 proposal，不能封存为候选。输入投影额外列出宿主计算的 `reference_policy.background_only_fact_ids`：来源为 qualified_failure 且归因 unknown 时，非合同片段只能作背景；单条检查 passed 不会把该来源升级为成功经验。最终仍由 domain 检查，不信任模型自觉遵守。

来源计划的最小形状如下。所有身份及路径必须来自 P1b 已完成的交付账本和实际回读，不能凭空填写：

```json
{
  "schema_version": 1,
  "project_id": "my-skill-project",
  "scope": {
    "service_identity": "<sha256-of-configured-memory-service>",
    "cube_id": "<source-cube>",
    "user_id": "<source-owner>",
    "namespace": "memos-skill-source-v1"
  },
  "sources": [
    {
      "evidence_path": "<absolute-evidence-json-path>",
      "evidence_revision": "<evidence-sha256>",
      "memory_ids": ["<actual-memory-id>"]
    }
  ]
}
```

示例只展示一项的字段；运行计划必须包含至少两个独立 lineage 的实际来源。本轮固定计划另保留完整失败记录，不能只选择成功行替代该验收样本。

真实图测试单独显式启用：`MEMOS_SKILL_GRAPH_TEST_CONFIG` 指向仓库外私有 JSON，字段为 `uri/user/password/database/memory_database_id`。运行 `pytest tests/skill_drafting/test_graph_live.py -q` 会写入唯一的合成测试项目并保留审计数据；未提供该配置时两项测试跳过。不能把原 Memory 连接作为此测试配置。

CLI 模块本身仅在具体操作时导入相关模块，直接运行 `src/memos/skill_drafting/__main__.py --help` 无需加载 SDK。标准 `-m` 入口仍经过项目原有 `memos/__init__.py`，其全局初始化及依赖行为不在本轮修改范围。现有初始化可能先向 stdout 写诊断日志，调用者读取最后一行 JSON，生成详情以 `job.json` 为准。

## 7 已知限制与 P3 入口

- 哈希绑定保证宿主记录的一致性，不是第三方签名。宿主、来源计划、文件系统、数据库凭据仍是受信任边界；不能把任意调用方可写的 JSON 当成收集器已认证的经验。
- 每条引用可解析不代表支持其结论。所有规则语义和适用性仍待审阅；现有两个 lineage 无法证明跨项目泛化、所有分支效果或稳定收益。
- 图仓储先读完整项目再检查事务，当前上限为 2000 节点、10000 边，逐条写入。适合本轮有限规模原型；规模扩大前需基准测试、索引查询和批量写入设计。
- 来源、文件和图之间没有分布式事务。导入前的二次回读限定检查时刻；后续 Memory 变化需 P5 的持续检测及可用性重算。
- 当前 domain/prompt/schema 固定为此版。回读工件会按当前实现重建提示与规范；后续更新必须保留版本化验证器或显式迁移，不能直接修改提示后把旧工件重新解释或误判损坏。
- P3 仍需 Python 管理接口、Go 权限与任务控制、React 审阅、候选隔离评测入口、人工裁决、绑定版本/验证的发布事务，以及普通 Agent 加载准入。P2 审阅 Markdown 不直接作为 Claude Code 的原生 Skill 加载文件。
- P4 再做固定版本的三组独立效果实验；P5 再做变更影响、撤回及依赖传播。合成管理测试的 Release 始终 `authorized=false`，不具备普通使用资格。

## 8 实际验收记录

### 8.1 真实提炼

实际重新回读 P1b 的 3 条 Memory：2 条成功、1 条完整失败，仍只有 2 个独立 case/lineage。输入包包含 61 个精确事实片段，模型公开投影明确标出 20 个只能作背景的片段。未使用 holdout，未重新执行 Claude 任务来增加样本，未向 MemOS 补写新的来源。

使用显式模型 `deepseek-v4.1-flash-1017`，请求与报告模型一致。开发过程中共执行 6 个独立生成作业：

| 作业 | 实际结果与处理 |
| --- | --- |
| 001 | 输出未完整结束，拒绝生成候选；预算 10000 |
| 002 | 输出未完整结束，拒绝生成候选；预算 16000 |
| 003 | 完整输出使用裸出口名，`draft_dangling_branch` 拒绝；补齐提示/schema 的 `exit:` 协议说明 |
| 004 | 完整输出遗漏必填 `tags`，`draft_proposal_fields` 拒绝；提示显式列出全部必填字段 |
| 005 | 将归因未知的失败来源中某个 passed 检查当作支持证据，`draft_unattributed_failure_support` 拒绝；输入显式增加背景引用清单 |
| 006 | 完整输出通过结构、引用、生成元信息和来源二次回读检查，原子封存为未发布候选 |

这些是跨提示修订的开发调用，不是固定提示成功率实验。所有失败目录均保留，没有手工修改模型 proposal，也没有通过放宽校验来制造成功。前两次尚未保存安全失败诊断，确切结束标识和用量无法补回；后续代码已补诊断，不能将已知调用用量当作六次总成本。

第 006 次调用耗时 59.064 秒，服务报告输入 21083、输出 12855、合计 33938 token。候选 `python-mock-fixture-contract-repair` 的 revision 为 `9a3b0c13435bbe8f4c578b6956307e0ea9f9a0f9c1327fbd8550db0cd8fd455e`，含 6 条规则、25 条精确引用、四类出口。1 条失败处置规则明确为 hypothesis/needs_evidence；其余规则也未获得独立任务验证。

工件保存于仓库外持久实验目录中的 `drafts/attempt-006/candidate/`，包括 packet、原始公开 proposal、generation、candidate、validation、manifest 和 REVIEW。连接配置、原始经验及运行工件均未进入 Git。

### 8.2 候选语义待审事项

结构合格不代表可发布。已将以下初步观察单独保存，未改写封存候选，也未替代人工裁决：

1. `verify_scoped_reads` 的验收列表只列出四项检查；来源完整验收还包括 `update_regressions` 与 `embedding_failure`。P3 应绑定完整验收合同，防止可见局部检查通过就宣布成功。
2. `confirm_contract` 可以根据合同存在进入修复规则，而后者条件又要求 guard 缺失。已有正确 guard、无需编辑时如何进入验证，仍需明确分支并独立验证。
3. 文本中的“有限修复轮次”尚未量化，需要与任务和适配器预算契约统一。

因此当前状态仍是 `semantic_review=pending`、`independent_validation=not_run`、`release_eligibility=not_evaluated`。这些实测发现就是进入 P3 审阅流程的输入，不能把代码审查 APPROVE 当成候选发布批准。

### 8.3 真实图及命令行联调

使用独立 Neo4j 5.26.6 容器及独立数据卷，Python driver 为 5.28.1。真实候选项目实际写入 **20 个节点、41 条边**：Skill 1、SkillRevision 1、Rule 6、MemorySnapshot 3、EvidenceRevision 3、Run 3、CaseRevision 2、结构 Validation 1。没有真实 Release 或虚构依赖。

通过真实 CLI 完成 inspect、graph-import、graph-inspect、trace 和 affected。重复导入同一候选新增节点/边均为 0；六条规则的 Memory/Evidence/Run/Case 链均可回读，三个 Memory ID 均能反查版本和规则，另一个项目查询为空。

实际重启独立 Skill Graph 容器后，数据库身份保持，项目图的规范内容摘要前后均为 `68ff70b3cb6b01527b7dd34b4c0c9d8d8466bda3c4b9edd8ade80c6db6212bb2`。原 Memory 数据库仍为 29 个节点、0 条边，三个来源重新回读的内容水位不变。另以原 Memory 的真实连接配置尝试初始化 Skill 仓储，得到 `graph_memory_database_forbidden`，没有建约束或写入。

独立合成管理项目实际验证全部九类节点和十四种关系，12 节点/16 边；其中 Release 仅为 `management_test/authorized=false`。两个独立 driver 并发添加相反 REQUIRES 时，一笔提交、一笔 `graph_cycle`，最终 2 节点/1 边。另验证不可变冲突、断链回滚、跨项目隔离、RELATED_TO 对称导航、限定范围的 SUPERSEDES、重连回读；这些合成数据不计入来源、候选收益或人工发布。

运行报告保存于仓库外 `reports/`：生成作业审计、候选图与幂等结果、逐规则追溯、Memory 反查、重启检查、错误数据库拒绝、代码审查和候选初步审阅观察。图追溯只读固定路径，不接受模型提供的 Cypher 或任意文件读取指令。

### 8.4 最终测试、审查与交付

使用已有 `.venv`，显式配置独立图测试连接，最终执行：

```text
.venv/bin/python -m pytest tests/skill_evaluation tests/skill_adapter \
  tests/skill_experience tests/skill_drafting tests/api/test_public_no_skill.py \
  tests/dream/test_persistence.py tests/api/test_update_memory.py -q
```

结果为 **701 passed in 87.93s**，包含两个真实 Neo4j 集成测试，没有跳过。Ruff check/format、pre-commit 和 `git diff --check` 通过。独立代码审查最终结论 **APPROVE**，并复核了实际生成期间增加的安全失败诊断、必填字段提示及背景引用清单；候选语义审阅仍是另一项未完成工作。

| 验收 | 结论及证据 |
| --- | --- |
| E01–E02 | 真实三来源、两独立 lineage，模型消费实际正文/事实并生成固定候选，见 8.1 |
| E03–E05 | 逐规则双类引用和资格约束通过；篡改、缺失、重复、来源变化均有拒绝测试；实际错误输出被阻断而未放宽条件 |
| E06–E09 | 独立图真实导入、幂等、追溯/反查、原子约束、九类合成管理关系、误连拒绝及重启通过，见 8.3 |
| E10–E11 | 701 项相关回归、静态检查、独立代码审查、本文及总开发计划更新完成；私有配置和完整工件留在仓库外 |

本轮中文功能提交为 `340c6c58`（来源提炼和候选工件）、`d95928c0`（独立图及 CLI）；实际生成推动的提示修正独立记录为 `538f6951`、`8a01259b`。范围与验收已先在 `bbd32572` 提交，最后另作结果文档提交。

**P2 达到本轮原型验收；整个 issue 尚未完成。** 当前已接通既有 Claude 来源经验到真实 Memory、候选提炼、固定版本工件和图追溯这一段。进入 P3 前不需要伪造发布状态；需要先处理 8.2 的候选审阅问题，并建设第 7 节列出的管理、隔离评测、人工发布和加载准入能力。
