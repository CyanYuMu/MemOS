# 42-A4e 组合与边界任务开发合同

日期：2026-11-09。基于 `codex/skill-prototype-branch-validation@75540c0a`，首增量开发分支为 `codex/skill-prototype-branch-extensions`。第 1–6 节先于实现冻结开发合同；第 7 节保留 A4e-1 当时验收，第 8 节记录执行协议与 schema 3 的后续进展。

## 1 当前依据与目标

两个独立核心任务已完成真实 Claude 运行与事实接入。scoped 裁决确认为修复成立但使用不当，不贡献覆盖；Dream 三个正常分支经用户确认后已登记，当前实际覆盖为 3/22、管理序号 23，仍未发布。

下一步首先验证同一次任务中的合同转换：两类缺陷同时存在，读修复通过后，Agent 是否根据仍然失败的 Dream 合同转入相应修复。不能把两个旧任务的各自成功拼成 `verify_scoped_reads#0` 的证据，也不能要求 Agent 为了覆盖而人为多修一次。

后续顺序是双缺陷组合 C1 → 读缺陷加健康 Dream C2 → 原本健康 H → 停止 S → 工具故障 F。共享代码家族的场景登记为同一谱系，不增加独立来源样本数。覆盖不成立、规则不可观察或缺来源时保留缺口，形成修订反馈，再衔接 42-B。

## 2 本次首增量 A4e-1

只实现 **C1 一个组合任务的资产、独立资产锁、物化与隔离自检**。它是合同驱动的新受控案例，不是生产系统的完整复制，也不是独立收益实验。

任务在同一工作区使用一个有状态 Fake 后端和两个明确的业务入口：scoped 文档修订与 Dream 动作更新。读端必须严格匹配 ID 与租户并返回深复制；UPDATE 必须使用字段字典、按 cube 写入并在非法参数时无副作用。两入口共享状态与写入记录，检查交叉影响。保留正确更新、owner/lifecycle、版本历史、embedding 失败、无新内容及 CREATE/ARCHIVE/MERGE 行为。

公开说明提供两类明确合同及检查分组；允许编辑业务 caller 与 Fake，保护任务说明、公开检查及辅助检查材料。初始缺陷必须分别可诊断：读范围缺陷，以及 UPDATE caller 参数和 Fake 拒绝能力缺陷。可见入口只物化初始文件；参考解、部分修复和错误解仅由宿主自检访问。

本增量不运行模型、不登记 Validation/assessment、不创建裁决或发布。不增加 HTTP、Go 或前端合同。自检结果明确标记 `purpose=branch_extension_asset_selftest`、`qualifiedAgentExperience=false`、`branchCoverage=[]`、`modelCalls=0`。资产自检通过不能代替未来执行协议的资格验证。

## 3 冻结边界与组织

新增 `src/memos/skill_branch_extensions/`，不修改旧 `skill_branch_validation` 的任何源文件或两任务锁，也不修改原适配器、P4 资产/实验、P4.1 执行器、旧经验证据、schema 1 domain/collector 和管理投影。现有旧报告、schema 1/2 事实及 H4/H6/scoped/Dream 裁决必须继续可回读。

组合任务使用独立套件 `combined-c1-v1`。每个套件固定自己的任务清单、资产摘要、检查分组、变体、预期失败、可编辑文件、目标分支、谱系及来源说明；身份只绑定选定套件，不绑定未来整个扩展目录。加载只核验锁，绝不自动重写锁。新增 C2 或健康套件不得改变 C1 身份。

可以调用已冻结的通用隔离执行与快照函数；不通过修改旧 registry、动态替换任务查找函数或重构旧 producer 复用代码。新自检保留执行器摘要与实际工件，但不宣称提供签名证明或 Python 同解释器反射的完整安全边界。

## 4 分组检查与部分状态

两组中的 `caller_contract`、`caller_with_fake` 同名，报告必须以稳定检查 ID 区分，例如 `scoped.caller_contract` 与 `dream.caller_contract`。每项同时登记所属组与 Skill 中的原检查名称，使用显式固定映射，不靠字符串猜测。公开说明解释映射；不能删去任一组的同名检查。

scoped 组保留六项：caller_contract、fake_scope_contract、scope_generalization、caller_with_fake、update_regressions、embedding_failure。Dream 组保留五项：caller_contract、fake_contract、caller_with_fake、other_actions、no_unintended_write。共享后端的交叉影响纳入两组对应验收；固定 oracle 分别用独立正确调用方/后端检查 caller 和 Fake，避免二者互相掩盖错误。

各命名检查默认使用新建状态；共享状态和跨入口副作用只在明确的交叉检查内部验证，不能由前一检查的写入影响后一检查结果。

资产自检至少固定六个完整工作区变体：

| 变体 | scoped 组 | Dream 组 | 作用 |
| --- | --- | --- | --- |
| initial | 指定检查失败 | 指定检查失败 | 两个初始缺陷可观测 |
| scoped_only | 全通过 | 仍有指定失败 | 支持未来观测“读已修好、Dream 仍有缺陷” |
| dream_only | 仍有指定失败 | 全通过 | 证明 Dream 修复不能掩盖读缺陷 |
| reference | 全通过 | 全通过 | 完整合同存在可行正确解 |
| unsafe_empty_read | 指定检查失败 | 全通过 | 从完整 reference 仅引入始终返回空的违规 |
| unsafe_update_arguments | 全通过 | 指定检查失败 | 从完整 reference 仅引入宽松 UPDATE 参数违规 |

实现时固定各变体的精确失败项与顺序，不仅检查整体退出码。两个 unsafe 变体必须从完整 reference 派生，不能让已有初始缺陷掩盖错误解的识别能力。六个变体均执行公开检查与隐藏 oracle；公开与隐藏的输入案例不同，但必须遵守同一合同。正确解若只对可见数据特判，隐藏检查仍应失败。

十二次是正式资产自检矩阵，不是整个测试阶段的执行上限。缺报告、超时、错导入及仅对可见数据特判等反向测试另行执行，不增加任务、模型经验或覆盖。

## 5 A4e-1 验收矩阵

| 编号 | 验收要求 |
| --- | --- |
| E01 | 新套件身份稳定，旧核心任务锁与 schema 2 producer 摘要不变；未知套件、被改资产或锁均拒绝 |
| E02 | 初始工作区只包含公开任务文件；无 oracle、参考解或错误解；已有目录拒绝覆盖 |
| E03 | 六变体 × 公开/隐藏共十二个实际隔离执行，检查顺序、完整性、退出码和精确预期失败一致 |
| E04 | 部分修复变体分别保留另一组的失败；不能用全局布尔成功掩盖分组状态 |
| E05 | 正常更新可用；错 ID/租户、非法 UPDATE、owner/lifecycle 拒绝和 embedding 失败均无意外写入；历史与其他动作保留 |
| E06 | 缺报告、缺检查、异常退出、超时、输出超限、候选被改或导入错路径均为自检错误，不能与预期业务失败混淆 |
| E07 | 受保护文件及文件清单固定；自检保留完整输入快照、任务摘要、执行工件和输出，公开/隐藏分别记录 |
| E08 | 自检不调用模型、不写图、不授予覆盖；新结果不能被旧 qualification 当作模型运行资格 |
| E09 | 已有三次核心运行与当前 schema 1/2 事实仍可回读；原 P4 52 份记录不改写 |

先补有意义的失败测试，再实现；使用现有 `.venv`，不改依赖。完成定向测试、Ruff、真实自检和代码审阅后再以中文提交实现及验收记录。此表在开发前冻结，实际验证结果另记于第 7 节。

## 6 后续增量及进入条件

**A4e-2 执行协议**：新增版本化 verifier、broker、资格矩阵。每次公开检查绑定完整快照、分组结果和原生送达；隐藏验收与初始/最终补丁绑定。模型运行前冻结选定套件、工具、预算、候选和导出。Agent 一次修好两类问题时可接受最终修复，但不能补写不存在的中间转换覆盖。

**A4e-3 事实与人工覆盖**：新 producer 和 schema 3，repository 仅新增明确分派；schema 1/2 保持原合同。组合分支要求同一 run/attempt 中的中间检查及快照，实际先修 scoped、观察 Dream 残余失败、确认转换时修复预算未耗尽，再采取 Dream 动作。条件、动作、规则声明、预算与检查分别绑定。新增事实不自动授予覆盖或发布。

**C2/H/S/F**：C2 要求读修复后重新检查原本健康的 Dream。H 要求初始和最终均正确且过程无无关编辑，修复数为零；不能仅靠最终相同哈希排除改后撤销。S 区分范围外、合同缺项、无法解决和有限修复后停止，保留实际失败及最终补丁，合理停止不等于修复通过。F 在运行前固定故障类型与调用序号，绑定实际送达和进入阶段时工具可用的证据；未执行的检查不能生成正常执行凭条。

上述增量先分别确定合同与验收后再实现。健康与停止需要新的结果类型，工具故障依赖停止合同和故障送达能力；不将它们硬塞进当前仅支持初始失败→最终通过的 schema 2。42-B 及 P5 的入口仍遵守治理开发合同，新增资产完成不代表这些阶段完成。

## 7 A4e-1 实现与实际验收

已新增独立 `skill_branch_extensions` 命名空间：`tasks.py` 管理套件身份、分组映射及仅初始资产导出；`combined.py` 提供共享状态任务、六变体和两套检查；`selftest.py` 管理完整快照、固定检查、执行异常与统一版本核验；CLI 提供 list / prepare / audit。`locks/combined-c1-v1.json` 已显式固定，加载不自动更新。provenance 明确派生于两个核心探针的实现模式，不作为第三个独立来源样本。

TDD 与独立审阅先实际发现，再关闭以下漏检：只记 scoped 写日志而不更新存储、存储嵌套字段与输入共享引用、MERGE 只记归档日志而不改变 source 状态。公开和隐藏检查均补实际完整状态与深复制断言；这三类错误解复验均在对应分组失败。另验证仅对公开 ID 特判的实现公开通过、隐藏失败。

运行结果缺失、检查重复/缺项、畸形状态、启动失败、后台进程与资源异常不能当作预期业务失败。每个入口固定起始套件摘要，核验物化、预期、执行行与结束回读一致；单次跨版本、矩阵混合版本、结束换版本和对象内部字典原地变化都不能汇总为通过。当前执行后端为 macOS Seatbelt；CLI 对不可用后端返回结构化错误和退出码 2，不退回无隔离执行，相应实际执行测试在其他平台跳过。

验证结果：

| 检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/skill_branch_extensions -q` | **45 passed，7.23 秒**；8 项资产/CLI/身份测试和 37 项独立反向测试 |
| Ruff 与显式新路径 pre-commit | 全部适用检查通过；未执行会修改全仓的格式化或依赖安装 |
| 正式 CLI audit | **12/12 符合预期**，suiteDigest `9f5e64776f12…`，bindingIntact=true |
| 完整参考解 | 公开/隐藏两次通过，两个分组均通过 |
| 初始、部分修复及 unsafe | 十次失败均符合精确检查列表；部分修复和 unsafe 的另一组仍通过 |
| CLI prepare | 仅导出 TASK.md、caller.py、fake_graph.py、visible_checks.py 四个初始文件 |
| 旧版本兼容 | 74 个冻结源码/锁文件字节不变；原 P4 52 份记录哈希不变；旧三次核心运行、双 skill 事实、none 双凭条/送达及四条人工裁决均能回读或重算 |
| 最终代码复审 | APPROVE；没有未解决的必改问题 |

正式自检工件保存在私有实验目录，模型调用、图写入、人工批准和新增分支覆盖均为 0。当前管理状态仍为 Dream 已确认的 **3/22** 覆盖、序号 **23**、发布 **0**，普通加载因 `release_unavailable` 拒绝。新的组合资产尚未获得 Agent 执行资格，不能用本次自检结果去登记独立任务覆盖。

复现入口（输出目标须为新目录）：

```bash
PYTHONPATH=src .venv/bin/python -m memos.skill_branch_extensions list --suite combined-c1-v1
PYTHONPATH=src .venv/bin/python -m memos.skill_branch_extensions prepare \
  --suite combined-c1-v1 --workspace /private/new-public-workspace
PYTHONPATH=src .venv/bin/python -m memos.skill_branch_extensions audit \
  --suite combined-c1-v1 --output /private/new-audit-directory
```

A4e-1 结束时的后续顺序为第 6 节所列执行协议、Claude、schema 3，再处理其余场景、42-B 和 P5；最新进展见下一节。

## 8 A4e-2/3 已交付与剩余门槛

组合执行协议、完整过程回读、schema 3 入图及浏览器审阅已经实现并实际联调。开发合同见[组合执行协议与证据接入](skill-prototype-combined-protocol.md)，完整测试、提交和实际批次见[验收报告](skill-prototype-combined-protocol-acceptance.md)。

真实 C1 none 与固定 Skill 两次会话均通过，均是一轮编辑后两组同时通过；没有符合条件的中间快照，不能补出中途转换覆盖。Skill 运行已登记 schema 3 事实，具体规则使用仍待人工裁决。当前覆盖保持 3/22、发布 0，normal 加载拒绝。

接下来按 C2/H/S/F 场景补齐合同与证据，处理其余分支和审阅挑战；再完成来源/工具变化的失效传播、新版验证和人工发布，满足门槛后才进入 P5。不能为凑覆盖重复已完成的 C1 会话或改写旧评分。
