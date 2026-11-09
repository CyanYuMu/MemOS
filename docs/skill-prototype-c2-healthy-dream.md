# C2：scoped 读缺陷与健康 Dream 开发合同

日期：2026-11-09。基线 `1da3f0a6`；开发分支 `codex/skill-prototype-c2-healthy-dream`。本文件先于实现提交，实际结果另记验收文档。

## 1 范围与成功标准

C2 验证修复 scoped 读缺陷后重新检查原本健康的 Dream，恢复正确行为并保留发现真实错误的能力。新增独立锁定套件 `scoped-healthy-dream-c2-v1`，任务 `branch-scoped-healthy-dream`。基于 C1 的共享状态资产派生：初始 caller 正确、Dream 字典参数及拒绝行为正确，仅 get_node 缺少租户守卫。初始公开和隐藏验收应恰有 `scoped.fake_scope_contract`、`scoped.scope_generalization` 失败，Dream 五项全通过。

最终必须 scoped 六项和 Dream 五项全部通过，包括真实持久状态、写入日志、非法参数拒绝、其他 Dream 动作和两入口共享状态的回归。公开与隐藏使用不同数据，宿主凭条绑定完整最终快照。隐藏验收在 Agent 退出后运行，不进入其可见目录或工具结果。通过最终补丁验收不等于正确使用 Skill、获得分支覆盖或证明收益。

本次包含资产、隔离协议资格、固定 Claude 执行、事实回读与 Neo4j 登记、人工审阅约束和浏览器展示。不修订候选，不发布，不重跑 C1，不补造中间转换；H/S/F、来源或工具失效传播、新版发布及 P5 继续保留后续门槛。

## 2 版本与兼容边界

C1 源码、锁和实际工件已进入摘要链，保持字节不变。新增 `skill_branch_c2` 独立入口，复用冻结的通用隔离/快照/原生采集函数及 C1 的资产常量；不修改旧 registry、不替换模块全局查找函数。需要绑定套件查找的协议实现从封存 C1 明确派生，使用 C2 自己的摘要和版本：`c2-receipt-1`、`c2-visible-1`、`c2-visible-result-1`、`c2-qualification-1`、`c2-process-1`、`c2-claude-1`、`c2-batch-1`、`c2-review-1`。

新事实为 schema 4，治理 repository 仅追加显式分派，旧 schema 1/2/3 的 producer 不变。页面追加 schema 4 的严格解析与展示，拒绝跨版本协议混用。复制少量已封存协议代码的代价是维护重复；本轮优先保证历史可回读，不在真实证据上进行通用引擎重构。未来抽取通用引擎必须另起版本和资格验证。

C2 与 C1、两个核心探针属于同一派生谱系：`newIndependentSamples=0`、非效果实验。实现前后检查旧源码/锁和 52 份 P4 原记录，回读 C1 资格、双会话、schema 3 assessment 和已确认的五条人工裁决。新增 C2 文件不得改变旧身份。

## 3 资格矩阵与负例

保留 11 项分组映射及检查语义。维护者参考变体仅供宿主资格验证，公开准备入口只导出 initial。

| 变体 | scoped 预期 | Dream 预期 | 验证目的 |
| --- | --- | --- | --- |
| initial | 仅两项守卫检查失败 | 五项通过 | 初始 scoped 缺陷、Dream 健康 |
| reference | 六项通过 | 五项通过 | 合同保持的修复 |
| unsafe_empty_read | 守卫/正常读取相关项失败 | 五项通过 | 禁止用空读掩盖错误 |
| unsafe_update_arguments | 六项通过 | fake_contract 失败 | 健康签名不能被放宽 |
| unsafe_dream_caller | 六项通过 | caller_contract/caller_with_fake 失败 | 正确 caller 被错误改坏必须检出 |
| unsafe_merge_sideeffect | 六项通过 | other_actions 失败 | Dream 其他动作的状态副作用必须检出 |

精确失败清单在锁内固定，由实际公开与隐藏执行共同核验；若上表与真实检查语义不符，先解释并补充开发合同，再冻结执行。每个变体各一次隐藏执行和一次真实 broker/client 调用，共 12 项资格。补充篡改凭条、错套件/快照/映射、保护文件修改、异常退出/取消、公开特例硬编码但隐藏失败等反例，不能仅测试参考补丁能通过。

## 4 固定执行与过程

保持候选 `f92905541710…`、投影 `management-native-3` 与原固定导出。运行前冻结代码、套件摘要、资格、CLI/模型/工具及预算，输出目录不可覆盖。首批最多两个真实会话：`c2-none` 校准成功后执行一次 `c2-skill`；失败保留事实，不为凑覆盖追加尝试。使用现有环境和私有 provider 配置，不安装依赖；每会话沿用 240 秒、12 turns、3 USD 上限及两轮编辑复验预算，运行清单以配置实值再次核验。

任务提示只给行为合同、全量检查及如实报告要求，不提示答案、目标分支编号或指定修复顺序。每次公开检查保留完整快照与原生送达；沿用保守编辑尝试批次计数，失败、无效、撤销编辑仍计数。C2 不要求额外人为中间状态：一次修复后的一次全量公开复验可以同时提供 scoped 和 Dream 的结果。`intermediateVerifications` 固定为空；所有公开 checkpoint 仍保存，初始/最终隐藏验收照常执行。若 Dream 过程中回归则如实保留，不能当作 C2 健康路径覆盖。

## 5 schema 4 与人工覆盖

结构保留 schema 3 的共同事实及分组 checkpoint，使用 C2 协议和独立 producer。schema 4 的 `initialVerification` 新增完整有序 `checks`，直接来自初始隐藏凭条；总状态 failed 无法单独证明 Dream 初始健康。修复候选要求初始隐藏/公开恰为预登记 scoped 缺陷且 Dream 全通过、完整加载/采集/送达/过程、最终公开及隐藏全通过、真实行为变化。错误或缺失信息保留 failed/incomplete，不自动授予 coverage。

预登记目标只有以下四项，不包含 `repair_dream_update#0` 或 C1 的 `verify_scoped_reads#0`：

| 分支 | 必须存在的本次过程依据 |
| --- | --- |
| confirm_contract#3 | 编辑前观察 scoped 基线失败和健康 Dream，阅读合同并声明选择 scoped 修复，随后真实编辑 |
| repair_scoped_reads#0 | scoped 缺陷观察→修复规则声明→真实编辑→完整 scoped 通过，预算不超过两轮 |
| verify_scoped_reads#1 | scoped 修复后的全量公开结果已经送达，scoped 六项与 Dream 五项通过；声明 Dream 合同仍适用且未发现剩余缺陷，并转入 verify_dream_update；声明不能倒置到结果之前 |
| verify_dream_update#0 | 修复后全量结果送达、健康 Dream 复验规则声明、明确成功退出，其后无未复验编辑 |

健康路径覆盖要求初始和每个已观察 checkpoint 的 Dream 组通过，不能把先损坏再修复包装成始终健康。声明 verify_scoped→verify_dream 的顺序需可追溯；不强迫无编辑时重复运行同一全量检查。检查引用必须精确到 `check:<序号>:<组名.检查名>`，规则和动作引用同一 run/attempt。判断语义仍由人工完成，最终修复、正确使用、分支覆盖分别裁决。事实导入与可审阅包生成无需新增批准；具体人工判断在用户确认后追加提交，不改变历史记录或发布状态。

## 6 工作包与验收门槛

1. **C2-A 资产/协议**：先写失败测试，再实现锁定套件与独立 verifier/broker/qualification；12 项真实隔离资格及安全负例通过。
2. **C2-B Claude/回读/治理**：独立 runner/process/batch/collector/importer、schema 4 分派；测试健康路径、损坏后恢复、声明时序错误、漏组/跨版本/伪轮次和旧版本兼容。
3. **C2-C 页面与实际验证**：页面解析和展示初始/最终分组、轮次、事件与待裁决；跑固定两会话，真实回读入图并在浏览器展示，不制造人工判断。
4. **C2-D 验收记录**：报告测试、两个真实会话、补丁行为、声明/过程限制、实际人工覆盖和剩余缺口；分中文 commit 提交文档、实现及验收。不得把计划目标四分支写成已覆盖。

使用现有 `.venv` 和定向 pytest/Ruff，改动涉及的完整既有回归及独立 Neo4j 测试；前端按仓库 `pnpm verify`。无 Go 逻辑变更则仅确认现有透传兼容。所有凭证、原生会话和私有部署文件留在仓库外。服务可由终端启动，前端必须在浏览器实际验证。
