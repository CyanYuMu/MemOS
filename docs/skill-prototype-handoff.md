# Issue #6 原型交付入口

日期：2026-11-09。对应 [Issue #6](https://github.com/hugegraph/MemOS/issues/6)。本原型完成从受控任务经验、Memory 回读、候选提炼与图谱，到独立验证、受托审阅和正式发布的链路。当前范围是 **Dream UPDATE 调用中移除多余位置参数**；不声称具备通用 Python mock/fixture 修复能力。

2026-11-11 本机增量：[可用性优化操作与验收](skill-prototype-usability-results.md)。新增常驻服务正常运行、未知反馈恢复、追加监测绑定及同合同新任务探测；下文保留原交付批次和 PR 的历史记录。本轮优化分支尚未推送。

## 1 当前交付结论

Python 数据面、Go 控制面和 React 审阅工作台可共同运行。真实 Claude 任务形成三条 MemorySnapshot，来源包含成功、失败及未确定归因；候选规则可追溯到固定来源和执行证据。新 `skill_p5` 七单元批次通过发布资格，六分支、三种停止边界完成“用户授权、助手受托审阅”，发布事务已产生真实 Release：

| 身份 | 值 |
| --- | --- |
| Skill | `python-mock-fixture-contract-repair` |
| revision | `c7fb3c895b2d49edc82c2808d551224d7c5ef705529d966b7973c6c042b3b8d6` |
| Release | `b23912814a518f795dd4598b3f009e514f779eda9873b8255ff7c81da84e951b` |
| 导出协议 | `management-native-3` |
| 当前执行/事实协议 | `skill_p5` / schema 7 |

正式发布后，Claude 的正常使用已实际证明加载与补丁通过。首次实际使用的反馈写入出现图中不可变任务身份冲突；原作业 `unknown` 保留，修复后独立回读追加了反馈验证，没有重写历史或增加独立覆盖。工具不可执行时 normal 被阻断，恢复权限后保持 `restored_pending`；变化之后的两单元恢复批次均通过，受托裁决及 restore 事务完成后才恢复准入。最终 normal 作业 `bb60be6bc37def5de240cafc2402f7177ee5d06c682eeb92c8e23a4f0831b7df` 为 `passed`，反馈 Validation `52a68ec08e2b878dfb3becc06b1af8d8cbfdf5ad19e945f36e684943813bd459` 已写回。最小发布—使用—失效—恢复—再次使用闭环完成；完整证据与浏览器观察见 [P5 验收结果](skill-prototype-p5-results.md)。

旧 e14 的覆盖 2/6、Release 0 是历史事实，原批次及裁决继续保留，不被新版覆盖追授。实验性的 `skill_release_v2` 尚未接入当前产品；其跨模块与来源 URI 展示缺口不等同于当前 `skill_p5` schema 7 未接入。

## 2 代码与职责

三个工作区分别交付到上游 `master`、`be`、`fe`，使用中文增量提交。分支如下；精确提交和 PR 以 Git 历史及最终交付记录为准。

| 工作区 | 交付分支 |
| --- | --- |
| Python 数据面 | `codex/skill-p5-minimal` |
| Go 控制面 | `codex/skill-p5-minimal-be` |
| React 前端 | `codex/skill-p5-minimal-fe` |

已向上游提交配套 PR：[数据面与完整交付 #7](https://github.com/hugegraph/MemOS/pull/7)、[Go 控制面 #8](https://github.com/hugegraph/MemOS/pull/8)、[前端工作台 #9](https://github.com/hugegraph/MemOS/pull/9)。三者分别以 `master`、`be`、`fe` 为目标分支，需联合评审/部署；本地验证通过不等于上游 CI 或维护者已批准。

| Python 模块 | 责任 |
| --- | --- |
| `skill_evaluation` | 固定任务、快照、隐藏验收及最终补丁绑定 |
| `skill_adapter` | 固定 Claude Skill 导出、隔离执行、原生事件采集；历史字节保留 |
| `skill_experience` | 受控来源任务、最小证据包、Memory 写入与回读 |
| `skill_drafting` | 来源引用、候选生成/修订、工件校验、Neo4j 图谱及追溯 |
| `skill_management` | 管理 API、固定审阅包、权限/事务、加载准入和正常使用反馈 |
| `skill_governance` | 执行事实回读、裁决、覆盖与历史记录处理 |
| `skill_lifecycle` | 来源/工具观察、依赖失效传播、恢复票据、撤回及替代 |
| `skill_p5` | 当前六类资格任务、七单元批次、恢复批次及 schema 7 事实导入 |
| `skill_release`、`skill_release_v2` | 保留的旧资格协议与未集成实验；不能代替当前执行入口 |

Go 提供现有会话认证、项目隔离、管理权限与同源代理；前端展示固定版本、来源、评测、裁决、覆盖、发布与生命周期。冻结协议的显式继任实现存在重复代码，便于复验历史，也增加了维护成本。

## 3 启动与演示

使用已有 Python `.venv`、Go、Node/pnpm，以及 Memory、独立 Skill Neo4j 和存储服务。配置与权限见 [操作手册](skill-prototype-operations.md) 和 [P3 配置](skill-prototype-p3.md)。凭据、部署路径及原生轨迹保存在仓库外。

在 Python 项目根目录运行，替换本机配置路径：

```sh
umask 022
.venv/bin/python -u scripts/dev/skill_p3_console.py --config /private/console.json
```

启动器输出带名称的服务日志。Ctrl+C 只停止本次启动的 Skill、Go 和前端，保留外部 Memory、Neo4j、Qdrant；已有实例运行时直接访问前端，不任意清理端口进程。

无需新模型调用的演示顺序：

1. 打开 `/cn/skills`，选择审阅项目和上述 revision，核对 content/export 摘要、Release 与管理序号。
2. 查看适用边界、两条规则、MemorySnapshot 和证据引用；区分成功经验、失败记录与宿主规范。
3. 查看六类资格任务、实际事件、最终补丁检查及六项受托裁决，核对 6/6 分支和三种停止边界。声明本身不是覆盖。
4. 查看正常使用与反馈记录，区分补丁通过、作业状态和独立覆盖；保留原 `unknown` 及追加恢复记录。
5. 查看工具不可用、`restored_pending` 与恢复记录，核对变化之后的新验证、准入及决定身份，以 [P5 结果](skill-prototype-p5-results.md) 为准。
6. 查看规则来源追溯与版本关系。依赖 DAG、多根因传播和撤回的隔离 Neo4j 测试单独标注；不把合成依赖节点称作真实提炼的 Skill。

实际导出示例位于 [examples/skill_prototype/p5](../examples/skill_prototype/p5/README.md)。重新执行资格及恢复批次使用 [P5 最小协议](skill-prototype-p5-minimal.md)；正常加载使用 `scripts/dev/skill_p5_normal.py`。所有输出目录与操作 ID 必须全新，不覆盖旧批次或反复抽取到成功。

## 4 验证与复现

自动测试、真实任务、浏览器观察及具体费用分别见 [P5 结果](skill-prototype-p5-results.md) 和 [交付记录](skill-prototype-delivery-record.md)。常用命令如下，数据库配置只能指向独立 Skill 测试环境：

```sh
MEMOS_SKILL_GRAPH_TEST_CONFIG=/private/graph-test.json .venv/bin/python -m pytest tests/skill_management tests/skill_lifecycle tests/skill_p5 -q --tb=short

# 前端工作区
pnpm verify

# Go 工作区
go test ./...
```

生命周期集成测试覆盖多根因、固定依赖、同版本工具字节变化、恢复不自动解禁、陈旧票据拒绝、暂停与撤回。真实工具变化最小演示与这些机制测试分别报告。Skill 私有服务没有 GET health；检查 Go `/healthz`、`/readyz`、Memory 健康端点及受认证 Skill 请求，不能把 Skill GET 的 501 当作故障。

## 5 实验结论与限制

[P4 三组对照](skill-prototype-p4-results.md) 的三个主修复任务，各组均为 3/3，未观察到修复率提升；Skill 组在这些任务上更慢、更贵。旧失败、验收器问题及 [P4.1 修正](skill-prototype-p41.md) 均保留。新七单元是发布资格验证，不是新一轮三组效果实验，也不增加独立问题族。

当前来源样本和场景很窄，不能证明广泛复用能力。候选经来源复核和受控修订收窄；成功补丁不证明模型每句话都正确，裁决明确排除无依据的细部解释。当前 P5 只演示专用工具副本失效，不冒称完成原双根因来源服务故障演示。`skill_release_v2` 跨模块接入、来源 URI 公共投影和实际 pending 并发窗口仍是后续工作。自动组合多个依赖 Skill、广泛任务研究、成本优化和协议公共层重构均未纳入本次交付。

正常编排器 `skill_management/p5_runtime.py` 的摘要写入运行 manifest，可追溯执行版本，但未进入本次冻结的生命周期监测清单。修改该文件不会自动触发工具失效；不能把当前工具扫描理解成覆盖所有宿主代码。后续扩展监测清单须建立新绑定并重新验证，不能改写旧基线。

## 6 文档索引

- [完整设计与决策](skill-extraction-design.md)、[开发阶段](skill-prototype-development.md)
- [来源与图谱](skill-prototype-p2.md)、[管理与发布](skill-prototype-p3.md)
- [治理与裁决](skill-prototype-governance.md)、[生命周期](skill-prototype-lifecycle.md)
- [当前 P5 开发与验收协议](skill-prototype-p5-minimal.md)、[P5 实际结果](skill-prototype-p5-results.md)
- [操作与故障处理](skill-prototype-operations.md)、[历史及最新交付记录](skill-prototype-delivery-record.md)
- [原 P5 扩展合同及范围调整](skill-prototype-p5.md)

管理决定统一注明“用户授权、助手受托审阅/执行”，不冒称用户逐项亲审。公开资料只包含安全摘要；完整私有证据由操作者保管。
