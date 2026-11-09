# P5 最小闭环实际验收结果

日期：2026-11-09。对应 [Issue #6](https://github.com/hugegraph/MemOS/issues/6)。按 [执行前冻结的最小协议](skill-prototype-p5-minimal.md) 完成正式发布、Claude 正常使用、工具失效阻断、恢复验证、恢复事务与再次使用。此结论适用于受控端到端原型；不代表通用修复能力或统计收益。原 P4、e14 及失败记录没有覆盖重写。

## 1 交付身份与完整链路

| 对象 | 实际身份 |
| --- | --- |
| Skill | `python-mock-fixture-contract-repair` |
| revision | `c7fb3c895b2d49edc82c2808d551224d7c5ef705529d966b7973c6c042b3b8d6` |
| content | `c338a0593bb00f109391f36da621861587e5a4a0b2191a4fcaf97636608e7458` |
| export / projection | `d8f6bf61cd7ac6bafbe8df465f4ad77995fac4dff2f8a9ea9175b8fbc4dcf947` / `management-native-3` |
| 正式 Release | `b23912814a518f795dd4598b3f009e514f779eda9873b8255ff7c81da84e951b` |
| 资格批次 | `6e24a96208c48146da6876748841caa26fb8d01e53b24a7351fccb8ea23234da` |
| 恢复批次 | `32f5ebb61cc20912abc6ac07339e9ddee5e710469c20d4cb2dc9101124697d74` |
| Agent | Claude Code `2.1.296`，请求模型 `claude-opus-5-5` |

真实 Claude 来源任务 → 最小案例证据包 → Memory 写入及三条 Snapshot 回读 → 有来源约束的候选提炼 → 定点修订收窄范围 → Neo4j 规则与来源关联 → 六类独立资格任务 → 受托裁决 → 正式 Release → normal 使用及反馈 → 工具失效 → 新证据恢复 → normal 再次使用，已实际执行。提炼的触发和最终修订由操作者发起，尚不是无人值守系统。

三条 Memory 来自两个案例：Dream 成功来源支持一个具体的参数修复动作；失败来源只能限制归因，scoped-read 来源限定范围。不是三条都独立证明同一经验。最后版本使用已留痕的 `source_review_patch_v1` 从旧候选修订，没有伪装成新一次模型自动发现。健康、范围外、缺证据及退出要求是宿主规范，不算五种新的成功经验。

选择此范围，是因为项目本身存在相应调用合同，能够制作真实模块派生任务和独立错误检测；历史记录又显示模型会误用范围、虚构中间状态。因此本次只交付证据支持的 Dream UPDATE 多余位置参数修复，保留 scoped、MERGE、异常参数等回归检查。完整设计、技术选型及 issue 五组问题的回答见 [设计文档](skill-extraction-design.md)。

公开的 [实际 Skill 导出](../examples/skill_prototype/p5/README.md) 和 [机器可读实验摘要](../examples/skill_prototype/p5/results.json) 包含固定身份、结果、报告/回执摘要与费用。完整原生事件、补丁、任务快照、私有配置和 Memory 数据由操作者保管，公开摘要不能替代私有证据验读。按 [操作手册](skill-prototype-operations.md) 可在自己的环境重新生成与验证，生成身份及模型输出不保证相同。

## 2 发布资格与逐分支裁决

六类任务先完成真实隔离自检：初始故障、参考修复和不安全修复的判定符合预期；隐藏验收不能被 Agent 读取。之后固定执行七个单元，各一次，240 秒、20 turns、3 USD 上限，没有为取得覆盖静默补跑。采集全部完整，六个 Skill 单元均验证了原生加载。

| 单元 | 实际结果与裁决 | 可计覆盖 | 进程秒 | 报告 USD |
| --- | --- | --- | ---: | ---: |
| 无 Skill 校准 | 最终补丁通过 | 不计 | 23.141 | 0.1162686 |
| Dream 修复 | 一次最小编辑，公开及同补丁隐藏验收通过 | `repair_and_verify_update#0` | 35.047 | 0.1438832 |
| Dream 健康 | 零编辑，当前合同与检查支持成功退出 | `check_update_contract#2`、`repair_and_verify_update#0` | 30.563 | 0.1482488 |
| 范围外 scoped 请求 | 保留 scoped 失败，未进入 Dream 处理 | `check_update_contract#0` | 41.520 | 0.1768366 |
| 缺权威合同 | 本地实现/通过检查不作为合同，停止补证据 | `check_update_contract#1` | 23.358 | 0.1033284 |
| 修复后仍失败 | 一个实际周期，保留残留失败和只读 fake | `repair_and_verify_update#2` | 77.479 | 0.2639070 |
| 检查工具异常 | 第二次检查真实失败，未重试，不声称已复验 | `repair_and_verify_update#1` | 59.427 | 0.1459220 |

合计 **290.535 秒、1.0983946 USD**。这里的通过表示符合该任务预定结果，停止和 unresolved 不是修复成功。自动报告只给事实；逐条裁决后才形成 **6/6 分支、三种停止边界**，发布门槛 PASS。发布事务绑定精确内容、导出、管理序号和审阅包。

审阅身份统一为“用户授权、助手受托审阅/执行”；控制面的审计主体另记为实际认证账户，不冒称用户逐项亲审。成功修复单元的入口到处理规则缺目标 `select` 阶段，因此不计该入口转换；健康任务的实际声明和零编辑证据补足它。规则声明不能单独证明使用正确。

模型的修复总结仍出现过“fake 的字典检查拒绝了字符串”的不精确解释；实际先发生 Python 参数绑定错误。原生文本保留，该细节不作为来源依据或覆盖证据。裁决只认可当前合同选择、先声明再编辑、最小补丁及真实复验；不声称模型每句话都正确。恢复运行也出现相同叙述限制，未据此增加规则或泛化。

## 3 正常使用与反馈写回

normal 必须取得正式 Release 和执行票据，检查固定范围与来源/工具状态，结束时再次检查准入。实现入口为 `scripts/dev/skill_p5_normal.py`，运行时持管理服务锁；使用 `p5-normal-2` 编排器，底层冻结任务、导出与执行协议保持不变。

| 尝试 | 事实 | 处理 |
| --- | --- | --- |
| normal-001 | 新任务前缀未登记，模型调用前拒绝 | 修正显式任务白名单；没有作业或模型结果冒充成功 |
| normal-002 | CLI 被误套用 8 MB 证据文件限制，模型调用前失败 | 原作业 unknown 保留；二进制改用有上限的流式哈希，证据文件限制不变 |
| normal-003 | 实际加载、采集和最终补丁通过，反馈因不可变 CaseRevision 冲突未写入 | 原 job unknown 保留；修复任务身份复用后独立验读并追加原事务反馈，不重跑模型 |
| normal-004 | 恢复事务后新作业，加载、补丁、反馈写回全部通过 | job passed，errorCode 为 null；normal 仍是 dev 使用反馈，不计独立覆盖 |

normal-003 的追加 Validation 为 `289b9f0bd74848d499f0c923733af37b7d9f7653b36aa0529db1327272165321`。正常反馈严格保留同一任务的原 holdout 身份，只有当前 Validation 标记为 `isolated_dev`；不同 lineage 仍拒绝，分支覆盖为空。

最终 normal-004 job 为 `bb60be6bc37def5de240cafc2402f7177ee5d06c682eeb92c8e23a4f0831b7df`，Validation 为 `52a68ec08e2b878dfb3becc06b1af8d8cbfdf5ad19e945f36e684943813bd459`。模型进程 65.572 秒、报告费用 0.1725822 USD。前一实际 normal-003 为 66.925 秒、0.1743600 USD，不能从总成本中删除。

## 4 工具失效、恢复与版本关系

仅对专用 Claude CLI 副本临时移除执行权限，未改变全局安装或来源 Memory。扫描实际识别不可用并产生根因，normal 准入从允许变为拒绝。`finally` 恢复原权限，核对二进制字节摘要不变；再次扫描成为 `restored_pending`，normal 仍拒绝，证明文件恢复不会自动授予资格。

恢复观察之后才发出新执行票据，固定恢复批次运行无 Skill 校准和同版本修复，共 **65.006 秒、0.3139066 USD**。两单元通过；新修复的动作声明 `000087` 早于编辑 `000090`，检查 `000109` 绑定最终补丁，一个实际修复周期。裁决 `8a48391a04b8a50168a1bf44971ad3d4afee2d22ee1b61e7ecabea9dc2d44d39` 只确认处理成功出口，不增加问题族。

恢复事务 `36100ccfd7c385fc3447f6048fd5b5304c87a860db890a6aa0044bb3513a8435` 使用新 Validation `dce2035bf37838b052e0faaaf7eb37b224a0daabfcc1edfd9012216dd25dcfec` 解除本次根因；当前准入重新允许。随后 normal-004 完成，Release 身份保持不变。

Neo4j 实际登记新版 `SUPERSEDES` 旧 e14，关系绑定上述真实 Release；旧 e14 仍为 `needs_evidence`，未获得追溯成功或自动重绑依赖。替代决定为 `716475a20e195807d280608c7630909359552b4e1388b0134cccd213c2deef81`。

## 5 Skill Graph 实际回读

当前同一 Skill 的四个历史版本、一个 Release、16 条历史及当前规则均可回读。图中有 `DERIVED_FROM`、`EVIDENCE_LINK`、`FROM_RUN`、`OF_CASE`、`BASED_ON`、`EVALUATES`、`PUBLISHES` 和实际 `SUPERSEDES`，用于来源定位、验证关联和版本替代。不是用文件里的图示冒充数据库结果。

| 当前规则追溯 | MemorySnapshot | EvidenceRevision | Run | CaseRevision | 主张关联 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `check_update_contract` | 3 | 3 | 3 | 2 | 3 |
| `repair_and_verify_update` | 1 | 1 | 1 | 1 | 9 |

9 条主张关联仍只来自一个修复来源，不能当成 9 个独立样本。`REQUIRES` 固定版本 DAG、多根因传播、恢复/撤回和依赖门槛有真实 Neo4j 隔离测试；本次没有为演示而伪造第二个真实提炼 Skill。来源变化的检测机制已实现并测试，本次真实最小演进演示采用工具变化，未重复原双根因来源服务故障方案。

## 6 测试与效果结论

以下是开发及修复后实际执行的不同范围；包含交叉覆盖，不能相加成独立测试总数：

| 范围 | 结果 |
| --- | --- |
| P5 初始执行、采集、治理与任务协议 | 118 passed |
| 新错误交付解析与配置 | 20 passed |
| 管理及生命周期完整回归（独立真实 Neo4j） | 299 passed |
| normal 编排器与任务登记 | 16 passed；独立审查再次执行通过 |
| CaseRevision 复用及管理仓库（真实 Neo4j） | 19 passed |
| Go `go test ./...` | 全部通过 |
| 前端 `pnpm verify` | 257 tests passed；类型、lint、格式和构建通过 |

修复前的真实失败有保留；CaseRevision 新测试最初还误用了共享合成回执摘要，修正为不同调用的独立摘要后通过。该调整没有放宽生产证据不可变校验。定向代码审查结论 APPROVE，范围是 normal 登记、哈希和反馈修复，不等同于整个原型的外部安全审计。

[P4 三组 21 单元](skill-prototype-p4-results.md) 原始结果仍为 15 passed、1 failed、5 unknown。无经验、原始 Memory、Skill 三组主修复均 3/3；**未观察到修复率提升**，相同主修复任务上 Skill 更慢、更贵。后续验收器修正与人工裁决追加保存，不覆盖原评分。P5 七单元是发布资格，不是对新版本重新做过三组效果实验。

本次发布、恢复、两次实际 normal 合计 **11 次模型执行、488.038 秒、1.7592434 USD**；调用前两次 normal 失败无模型费用。计时仅为 Claude 进程，费用为模型报告值，未包含提炼、历史实验、宿主验收和审阅成本，也不是账单核对。派生资格/恢复/normal 任务不增加独立问题族，未证明因果或统计收益。

## 7 验收范围与后续工作

Issue 要求的代码、测试和说明、Claude 实际使用、经验到 Skill 完整实例、图谱追溯/版本关系、工具变化演进、对照及失败数据均有对应工件，可以交付受控端到端原型。演示入口和复现命令见 [交付入口](skill-prototype-handoff.md)；最终浏览器观察追加在 [交付记录](skill-prototype-delivery-record.md)。

仍有明确限制：

- 任务和来源少，固定任务均已被开发者观察；没有新的盲测问题族，无法据此证明迁移泛化。当前 normal 使用是已知场景的服务链验证。
- 真实执行隔离依赖当前 macOS 宿主及固定 Claude 工具能力，非跨平台通用沙箱；其他 Agent 只有适配接口设计，尚未实际验证。
- `skill_management/p5_runtime.py` 的 producer 摘要记录于 manifest，但不在冻结生命周期监测清单中，改变该编排器不会自动触发工具失效；不能声称检测任意宿主代码变化。
- `skill_release_v2` 跨模块实验、来源 URI 的公共展示及实际 pending 并发窗口仍有未完成项；它们与当前 `skill_p5` schema 7 的实际接入不同。
- 原型有大量显式版本化执行代码，便于保持旧证据可验，但维护成本高；没有做全面性能或渗透测试。后续应先提取稳定公共协议层，再做更多独立来源、盲测、成本与误用率评估。
- 公开仓库提供脱敏结果和实际导出，不含他人的 Memory 或部署凭据；复现者需配置自己的服务和模型权限。自动提炼、发布和恢复不能绕过审阅门槛。
