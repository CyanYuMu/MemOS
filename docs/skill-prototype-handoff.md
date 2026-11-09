# Issue #6 原型交付入口

日期：2026-11-09。对应 [Issue #6](https://github.com/hugegraph/MemOS/issues/6)。本次按用户要求收口为基本可用的受控原型，提供真实经验、候选产出、Claude 使用、效果记录及治理机制；不把尚未满足的发布资格写成完成。

## 1 交付结论

当前可运行的是 Python 数据面、Go 控制面和 React 审阅工作台。已经用真实 Claude 任务采集经验，保存并回读 MemOS，生成候选和规则来源关联，写入独立 Neo4j Skill Graph，在固定任务中使用候选并回收结果，完成具体记录的受托裁决。旧失败、unknown、错误泛化与历史挑战均保留。模型生成与实际任务存在失败，不据小样本宣称成功率或成本优势。

当前 e14 候选处于 `needs_evidence`，六项验证、六项裁决、覆盖 **2/6**、正式 Release **0**。未发布候选的 normal 加载被拒绝，属于已验证的准入行为。发布事务和失效传播有代码及集成测试，但真实发布后的 P5 使用—失效—恢复闭环未完成。

本轮不再扩展跨模块验证家族。`skill_release_v2` 是有测试的实验实现，尚未接入工作台；首次 v2 生成在发送前被阻断，调用数为零。原 Memory 中本机证据 URI 的展示脱敏尚待处理，不能宣称已解决。当前服务继续使用原 schema 1 候选、schema 1–6 审阅合同和 `management-native-3`。

## 2 代码与版本

| 工作区 | 交付分支 | 实现基线 |
| --- | --- | --- |
| Python 数据面 | `codex/skill-prototype-protocol-v2` | `1867e1c6`，后续收尾文档提交见 git log |
| Go 控制面 | `codex/skill-prototype-lifecycle-be` | `7c0d2701` |
| React 前端 | `codex/skill-prototype-lifecycle-fe` | `dc76b147` |

均采用中文增量提交；未推送、合并或改写历史。三个工作区需要一起交付，不能只提交 Python 分支并声称包含控制台。

| Python 模块 | 责任 |
| --- | --- |
| `skill_evaluation` | 固定任务、快照、隐藏验收及最终补丁绑定 |
| `skill_adapter` | 固定版本 Claude Skill 导出、隔离执行、原生事件采集；历史生产字节保留 |
| `skill_experience` | 受控来源任务、最小证据包、Memory 写入与回读 |
| `skill_drafting` | 来源筛选/引用、候选生成与修订、工件校验、Neo4j 图谱及追溯 |
| `skill_management` | 管理 API、候选评测、固定审阅包、权限/事务和加载准入 |
| `skill_governance` | 执行事实回读、裁决、覆盖与历史记录处理 |
| `skill_lifecycle` | 来源/工具观察、依赖失效传播、恢复票据、撤回及替代 |
| `skill_release` | 六类固定资格任务、七单元批次、评分及事实导入；真实结果有失败，未授权发布 |
| `skill_release_v2` | 新门禁/原生传输及合同上下文候选实验；不是当前产品执行入口 |

Go 负责现有会话认证、项目隔离、管理权限与同源代理；前端负责固定版本、来源、评测、裁决、覆盖、发布阻断及生命周期的展示和操作。

## 3 启动与十分钟演示

使用已有 Python `.venv`、Go、Node/pnpm，以及 Memory、独立 Skill Neo4j 和现有存储服务。配置格式、权限和日志约定见 [操作手册](skill-prototype-operations.md) 与 [P3 配置](skill-prototype-p3.md)。凭据和本机部署路径放仓库外，不提交原生轨迹。

在 Python 项目根目录运行，`/private/console.json` 替换为本机配置路径：

```sh
umask 022
.venv/bin/python -u scripts/dev/skill_p3_console.py --config /private/console.json
```

启动器输出带服务名称的日志。Ctrl+C 只停止它启动的三项服务，保留外部 Memory、Neo4j、Qdrant。已有实例运行时直接访问其前端；不重复启动，也不任意清理占用端口的进程。

演示顺序：

1. 打开前端 `/cn/skills`，选择审阅项目与 e14 固定候选，核对 revision、导出摘要、管理序号。
2. 查看适用条件、两条规则及来源片段。展开 MemorySnapshot/证据版本，区分来源成功、失败和未知归因。
3. 查看六条独立验证及已有六项裁决。对比健康零编辑、补丁通过但规则使用不当、合理停止、工具错误与捕获不完整。
4. 展开分支覆盖矩阵，确认只有实际支持的 2/6。查看尚未解决的挑战，不把声明当作覆盖。
5. 检查 normal 加载准入，应拒绝并显示 `release_unavailable`；发布记录为零。
6. 查看生命周期主体和依赖绑定，核对来源/工具实际摘要。用隔离测试项目演示变化传播和恢复门槛，不向真实候选添加演示依赖，不把合成 Release 当作实际许可。

上述步骤不需要新模型调用。需要重新执行任务时，遵守 [独立验证合同](skill-prototype-release-validation.md) 的新批次、固定版本、独立验收和预算要求；不要覆盖已有目录或重复抽取到成功为止。

## 4 验证和复现

已实际确认：前端页面可访问；Go `/healthz`、`/readyz` 正常；Memory 健康且三来源回读一致；Neo4j 连接及项目图可读；Skill 私有 POST API 拒绝无效认证。Skill 服务没有 GET health 接口，不能以 GET 返回 501 判定服务故障。

本轮最终自动检查与真实观察记录见 [交付记录末尾](skill-prototype-delivery-record.md)。常用复现命令：

```sh
# Python：配置只能指向独立 Skill 数据库；每个测试创建独立项目并保留审计数据。
MEMOS_SKILL_GRAPH_TEST_CONFIG=/private/graph-test.json .venv/bin/python -m pytest tests/skill_management tests/skill_lifecycle tests/skill_drafting/test_graph_live.py -q --tb=short

# 前端工作区
pnpm verify

# Go 工作区
go test -race -shuffle=on -count=1 ./...
```

生命周期集成测试覆盖多根因传播、固定依赖、同版本工具字节变化、恢复后不自动解禁、陈旧/不匹配票据拒绝、暂停与撤回，以及合成许可不能授权真实发布。它们验证实现机制；不是已在真实发布候选上完成 P5 的证据。

## 5 实验结论和未完成项

三组固定版本实验保留在 [P4 结果](skill-prototype-p4-results.md)，验收器修正见 [P4.1](skill-prototype-p41.md)。后续 `release-batch-001` 七次真实调用全部保留，合计 282.468 秒、报告费用 1.1433588 USD；计划资格未通过。健康子场景提供 2/6 分支，其他记录存在时序、范围或错误送达缺口。失败结果仍具有判断规则适用性和执行协议问题的价值，不能用于证明收益。

| 未完成项 | 为什么保留 | 后续必要动作 |
| --- | --- | --- |
| 真实候选正式发布与正常使用 | 目前独立覆盖/裁决不足，发布门禁正确阻断 | 固定窄范围新版本，补足必要资格并审阅后发布 |
| P5 发布后演进闭环 | 依赖实际有效 Release | 按原 P5 合同运行，不用集成测试替代 |
| v2 候选实际生成 | 请求前发现本机来源 URI 的展示问题 | 确定性公共投影与原事实身份分离，重新冻结清单 |
| v2 完整接入 | 固定候选导出/加载、schema 7、authority 写前检查未接线 | 单独实施，保留旧版本行为 |
| 实际 pending 并发窗口 | 四次协议批次中未观察指定调度 | 保留确定性反例和真实顺序，不能声称实际窗口已覆盖 |
| 普遍效果和成本优势 | 小样本、同机制派生任务不足以支持 | 后续使用更广独立任务，不将资格验证当效果研究 |

因此本次可以交付受控原型及可信的正反结果，不能以此声明“真实发布与 P5 已全部完成”或把原发布挑战关闭。

## 6 文档索引

- [完整设计与决策](skill-extraction-design.md)
- [开发阶段](skill-prototype-development.md)
- [来源与图谱](skill-prototype-p2.md)
- [管理界面与发布合同](skill-prototype-p3.md)
- [治理与裁决](skill-prototype-governance.md)
- [生命周期](skill-prototype-lifecycle.md)
- [操作与故障处理](skill-prototype-operations.md)
- [真实运行、费用和受托裁决](skill-prototype-delivery-record.md)
- [尚未完成的 P5 验收合同](skill-prototype-p5.md)

管理决定统一注明“用户授权、助手受托审阅/执行”，不冒称用户逐项亲审。公开文档只包含安全摘要；完整私有证据由操作者本机保管。
