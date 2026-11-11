# Issue #6 验收对照

日期：2026-11-11。对照 [Issue #6](https://github.com/hugegraph/MemOS/issues/6) 的“验收时希望看到”和阶段三交付物，逐条给出当前状态和可核对的依据。✅ 表示有直接证据；⚠️ 表示有证据但强度或对象不足，原因写在同一行。本文件只汇总，不替代各阶段原始记录。

## 1 验收项

| issue 验收项 | 状态 | 依据与不足 |
| --- | --- | --- |
| 能解释为什么选择当前场景、方案和实现范围 | ✅ | [设计文档](skill-extraction-design.md) §2.4 场景选择、§13 选型与替代方案、§16 问题映射；[交付入口](skill-prototype-handoff.md) §1 说明为何收窄为 Dream UPDATE 多余位置参数 |
| 从多条经验产出的 Skill 能追溯依据，并能在选定 Coding Agent 中实际使用 | ⚠️ | 已发布 c7 的两条规则可经 `trace_rule` 追溯到 3 条 MemorySnapshot、3 份证据、3 次运行、2 个案例；Claude Code 2.1.296 正常加载并回写反馈（[P5 结果](skill-prototype-p5-results.md)）。**不足**：支撑修复动作的 Dream 成功经验只有 1 条，另两条是失败经验与范围经验，不能称为多条独立成功经验 |
| 能展示 Skill 与来源 Memory、Skill 与 Skill 的关系及其用途 | ⚠️ | Skill↔Memory 为真实数据：`DERIVED_FROM`、`REFERENCES`、`EVIDENCE_LINK`，以及来源变化时的反向定位 `affected_by_memory`。Skill↔Skill：真实图只有 1 条 `SUPERSEDES`（c7 替代 e14，携带 Release 审批引用）；`REQUIRES` 依赖 DAG、`RELATED_TO` 与多根因传播只在明确标记的合成 Neo4j 项目中验证（[P2](skill-prototype-p2.md)、[生命周期](skill-prototype-lifecycle.md)），没有为演示伪造第二个提炼 Skill |
| 有可复现的效果验证，包含合理的对照和失败案例 | ⚠️ | P4 三组对照、U4 同合同迁移对照、U5 对已发布 c7 的契约审阅，以及 11 月 11 日在 c7 上新增的边界任务对照（[结果](skill-prototype-boundary-ab.md)）。失败与 unknown 均保留、未剔除（P4 为 15/1/5）。**不足**：三次对照都没有区分度——P4 主任务三组 3/3，U4 三组 2/2，边界对照 12 个配对两组全部通过；两两间也无统计效力，因此目前只能说明“在这些任务上 Skill 没有可测量的边际价值”，不足以证明有效或无效 |
| 能说明 Memory 或工具变化后如何识别并处理受影响的 Skill | ✅ | 工具变化有真实演示：专用 CLI 失去执行权限 → 准入拒绝 → 恢复权限后 `restored_pending` 仍拒绝 → 新验证与 restore 事务后才恢复准入（[P5 结果](skill-prototype-p5-results.md) §4）。来源 Memory 内容、来源与归属变化的检测已实现，用真实 Neo4j 隔离测试覆盖，未在已发布 Skill 上做真实演示 |
| 代码结构、测试、错误处理和文档足以支持他人理解、运行与继续迭代 | ✅ | 全部为新增 `skill_*` 模块，未改动现有 `memories`、`dream`、`api`、`graph_dbs`；Skill 测试覆盖采集、提炼、图谱、管理、生命周期与实验；结构化错误码且不回显私有信息；[公开复现指南](skill-prototype-public-reproduction.md) 提供不依赖作者私有工件的分阶段命令和无需模型的离线自检 |
| 对已知限制、未完成部分和下一步有清晰判断 | ✅ | [交付入口](skill-prototype-handoff.md) §5、[P5 结果](skill-prototype-p5-results.md)、[契约复现结果](skill-prototype-contract-repro-results.md) §4 及本文件 §3 |

## 2 阶段三交付物

| 交付物 | 状态 | 依据 |
| --- | --- | --- |
| 代码、必要的测试与使用说明 | ✅ | 见上表第六行 |
| 至少一个 Coding Agent 的接入和使用示例 | ✅ | Claude Code 固定原生导出 [examples/skill_prototype/p5](../examples/skill_prototype/p5/README.md)；正常加载入口 `scripts/dev/skill_p5_normal.py` |
| 从多条经验到 Skill 产出、使用和效果验证的完整示例 | ⚠️ | 经验 → 产出 → 使用完整；效果验证见上表第四行 |
| Skill Graph 相关能力的演示 | ✅ | 规则溯源、来源反向定位、幂等导入、版本替代均可在真实图中查询；管理图须用 `ManagementRepository` 读取，基类 `SkillGraphRepository` 会拒绝管理记录 |
| 至少一个涉及来源变化或工具变化的演进示例 | ✅ | 工具变化真实演示，见上表第五行 |
| 实验数据、结果分析、已知限制和后续改进方向 | ✅ | 各实验均有机器可读摘要：[P4](../examples/skill_prototype/p4/README.md)、[P5](../examples/skill_prototype/p5/README.md)、[U4 迁移](../examples/skill_prototype/usability-v1/results.json)、[边界对照](../examples/skill_prototype/boundary-ab/README.md)；完整原生轨迹由操作者保管，公开文件用哈希绑定 |

## 3 尚未达到、需要后续工作才能关闭的部分

1. **多条独立成功经验**：需要再采集至少一条独立谱系的 Dream UPDATE 成功经验并修订候选。
2. **真实的 Skill↔Skill 依赖**：需要第二个真实提炼并发布的 Skill，才能展示非合成的 `REQUIRES`/`RELATED_TO` 及其失效传播。
3. **来源 Memory 变化的真实演示**：机制与测试已有；在已发布 Skill 上演示会暂停其准入，需要恢复验证的模型调用，本轮未做。
4. **有区分度的效果实验**：需要一类无经验组会失败的任务，或在现有任务上先解决“检查与编辑顺序”无法证明的执行器缺口，否则同场景继续加样本仍会得到无差异结论。
