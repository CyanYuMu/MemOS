# P0b：真实项目任务与独立验证资产

日期：2026-11-08。依据：[总开发计划](skill-prototype-development.md)、[冻结设计第 11 节](skill-extraction-design.md#11-固定任务与效果评估协议)。本文件在实现前固定范围与验收标准，实施后的证据另行补入。

## 1 交付边界与开发顺序

本轮完成 P0b：两个真实项目来源任务 S1/S2、七个独立验证任务 H1–H7、用途与谱系注册、可见文件导出、固定资产清单和外部验收自检。复用 P0a 的快照与回执。任务资产就绪不等于已取得 Agent 经验，也不等于 Skill 已有效。

先提交本文；再开发固定任务资产与行为测试；随后接入注册表、回执和 CLI；最后运行全组资产自检、相关回归、代码审阅并记录结果。开发分支 `codex/skill-prototype-p0b` 基于 P0a 的 `eed6d186`，保留原分支，按功能作中文 Conventional Commit，不改写旧历史，不推送或合并。既有未跟踪 `bin/` 不属于本轮。

真实任务采用锁定提交 `5a595b4a06676c8d259412ae75048fdb668c482c` 中的生产定义副本，并显式记录复制符号、源文件哈希、导入适配和本地依赖替身。验收运行实际复制的函数，不使用概念性 caller 代替生产函数。为离线运行而替换的模型、日志或服务构造要在 provenance 中说明；本轮不宣称覆盖完整 HTTP/数据库服务。

## 2 固定任务

| 任务 | 起源与场景 | 初始缺陷及期望 | 外部验收重点 |
| --- | --- | --- | --- |
| S1 | DreamPersistence 的真实持久化调用 | 注入 caller 的 update_node 错误打包；Fake 保持严格；修复 caller | 独立合规后端、Fake 正反合同、实际写入与其他 action 回归；拒绝放宽 Fake 或吞掉错误 |
| S2 | handle_update_memory 的真实租户更新路径 | caller 正确；Fake 注入跨租户/ID 回退；修复 Fake | 本租户有效更新、外租户/缺失 ID 无写入、租户解析与状态/元数据/嵌入回归；拒绝总返回空 |
| H1 | 独立 Record 查询消费场景 | Fake 返回旧字典，caller 合规 | Fake 返回类型、caller 对另一合规实现、端到端；拒绝只让 caller 兼容错误 Fake |
| H2 | 独立查询截止时间场景 | caller 将毫秒传入秒参数 | 多输入单位换算与范围、Fake 拒绝非法参数；拒绝常量或放宽参数 |
| H3 | 独立图接口迁移场景 | caller 旧关键字与 Fake 旧返回结构同时错误 | 两侧分别对独立合同及集成；拒绝双方私约、只修一侧 |
| H4 | 独立有状态测试场景 | session fixture 泄漏跨测试状态 | 实际 pytest fixture 与多种顺序运行；Fake 的累积状态行为仍正确；拒绝常量返回或删测试 |
| H5 | 独立业务折扣场景 | 双方合同正确，业务边界错误 | 边界两侧及双方合同；拒绝改 Fake 数据迎合算法 |
| H6 | 独立字段语义冲突场景 | 没有权威字段语义 | 自动检查代码未擅改和诊断材料结构；具体缺失定义、依据与停止理由须人工判断 |
| H7 | 独立对象装配场景 | 对象过早构造并捕获旧依赖 | 实际依赖身份、目标替身参与和原行为；拒绝只改未使用 Fake |

H1–H7 是不同业务组件与故障谱系的受控近真实任务，不是 MemOS 全服务生产故障。制作者标签、参考补丁、oracle、评分量表与本文件仅用于评测侧，不能导入 Agent 工作区。参考修复只用于检查验证器，不算来源经验。S1/S2 与 P0a dev 探针存在机制联系，显式登记，不作彼此独立收益证明。

H4/H5/H7 的自动行为通过只代表整体代码问题解决；是否正确识别 Skill 范围仍需轨迹与人工评分。H6 没有可自动证明的正确修复：合格的静态提交最高为 `needs_review`，`repair_pass=null`、`boundary_pass=null`，不能直接变为 passed 或 verified_fixed。干净基线对 H6 仅指保留原程序、附有待审诊断材料；不是已修复原本失败的可见断言。验收器使用两源码文件的字节不变作为保守守卫，不宣称实现了语义等价分析；猜测性改动和无害注释改动都会被要求撤回。本规则仅在评测侧描述，任务可见材料不给出预期答案。

## 3 资产与注册表契约

- 注册表提供固定 case ID、revision、split、lineage、origin/provenance、关联 dev 谱系、评分模式、允许修改路径、必须导入模块及固定检查集合。每次读取返回独立数据，不向模型提供修改用途的接口。
- 固定清单包括所有初始/基线/参考/错误修复内容摘要、oracle 与合同摘要及注册元数据摘要。运行与自检验证清单；内容变化必须更新 revision 和清单，经测试审阅，不能依赖运行时仓库 HEAD 悄悄改变任务。
- 跨用途共享故障谱系，尤其 source/dev 与 holdout 重合，必须阻断；独立性另经人工核对来源、代码与缺陷机制，不能仅凭 UUID 或不同字符串证明。
- 导出采用正向文件清单，不复制仓库目录、Git 历史、生成器、隐藏评分或其他任务材料。候选中受保护任务说明、合同、可见测试或未知文件变化应被检测；允许修改路径外的变化不得获通过回执。运行产生的缓存需明确处理，不能成为秘密输入。
- 评测侧保存冻结资产清单，候选目录只含任务说明、必要合同、待修复实现和可见测试；manifest 的 split 与谱系由可信侧解释，修改候选文件不能改变任务身份。
- 本轮的文件拆分不构成操作系统访问隔离。进入 Claude Code 前，P1 必须阻止 Agent 读取整个评测仓库、隐藏资产、工件目录与其他组轨迹，不能直接在本仓库根运行正式 holdout。

## 4 验收与评分

| 编号 | 必须满足的条件 |
| --- | --- |
| B01 | S1/S2 可追溯至固定项目提交和生产符号；副本转换可检查，生产源文件不被修改 |
| B02 | S1/S2 的健康基线与正确修复通过；初始故障产生预期行为失败，而非导入/环境错误 |
| B03 | H1–H5/H7 健康基线与至少一种合规修复通过；初始故障在对应检查失败 |
| B04 | 九任务各有掩盖错误/错误归因的负例，外部验收拒绝；不能只依靠标准补丁相等判断 |
| B05 | H4 执行真实 fixture 生命周期并覆盖顺序；H7 核查实际对象参与，不能只凭零调用次数猜测 |
| B06 | H6 初始缺少诊断材料不通过，参考提交仍待人工审阅，猜测性改代码失败；不生成自动修复或边界成功分数 |
| B07 | 注册表和固定清单可重复验证；重复 ID、谱系污染或内容漂移必须拒绝；source/dev 不能重标 holdout |
| B08 | 导出文件清单可检查，不包含隐藏 oracle、参考方案、制作者标签、完整仓库或 Git 历史 |
| B09 | 改受保护合同/可见测试或新增未知输入不能通过；合法替代实现可按行为通过 |
| B10 | 回执绑定实际 split、lineage、资产摘要、初始快照和候选快照；改代码、任务或评分依据后旧回执失效 |
| B11 | 报告分别记录自动行为结果、待人工项和经验资格；所有人工参考运行 `qualified_agent_experience=false` |
| B12 | CLI 可列 source/holdout、导出初始任务、验收、检查回执并执行全组资产自检；P0a 命令仍可使用 |
| B13 | P0a 测试、相关 Dream/API/公共 SkillMemory 回归和改动范围 Ruff 检查通过；报告实际命令和结果 |

任务自检检查健康基线、初始故障、参考提交和 unsafe；参考与基线可能内容相同，但角色分开记录。这些运行不计入七任务三组的 21 次正式实验，也不形成真实 Agent 成功率。

H4 的支持范围限定为候选 `graph` fixture 的生命周期与普通辅助代码；不支持候选额外注册 pytest 执行/收集 hook 或插件来改变外部测试。此约束也写入任务可见合同。外部测试只接入所需 fixture，并核对预期测试的实际阶段结果，拒绝跳过、xfail、删除测试或替换测试体的常见捷径。它仍不是任意恶意 Python 的隔离沙箱；可信验证进程与 P1 运行权限边界不可省略。

评分以固定设计第 11.4 节为准。本轮可给出行为修复字段，但 H4–H7 的范围判断、规则使用与严重违规归因不从测试通过自动推断；保留待审字段和量表。完整三组预算、模型与隔离配置由 P1/P4 校准锁定，缺失时不能启动正式效果实验。

## 5 距离 Claude Code 接入的后续门槛

P0b 后应具备任务输入与最终产物验收，P1 仍需：固定 CLI/SDK/模型及配置；原生固定版本 Skill 加载的实测证据；Agent 可见目录与隐藏评测权限隔离；实际工具/步骤/尝试/费用采集；Run 初始状态到最终补丁与 receipt 的关联；脱敏、失败/超时/后台进程回收；source 与 holdout 轨迹分别保存；实际运行报告与 MemOS 写入回读。人工参考补丁不能补足这些缺口。

## 6 实施记录

当前状态：**P0b 已完成**，B01–B13 均有对应测试或资产自检证据。尚未启动真实 Agent 任务采集，未写入 MemOS，未进行 Skill 效果评估。

在现有 Python 3.12.13 环境执行，未新增依赖，未修改生产 Dream/API 代码：

| 检查 | 最终实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/skill_evaluation -q` | 212 passed，48.11 秒；包括原有 P0a 与本轮新增 124 项测试 |
| `.venv/bin/python -m pytest tests/api/test_public_no_skill.py tests/dream/test_persistence.py tests/api/test_update_memory.py -q` | 39 passed，2.99 秒 |
| `.venv/bin/python -m ruff check src/memos/skill_evaluation tests/skill_evaluation` | All checks passed |
| `.venv/bin/python -m ruff format --check src/memos/skill_evaluation tests/skill_evaluation` | 18 files already formatted |
| 实际 CLI `audit-tasks` | 退出码 0；9 任务、36 次验收的结果与失败检查均符合预期；全部回执匹配；9 次候选变化均使旧回执失效 |

CLI 报告中，S1/S2、H1–H5/H7 的“基线 / 初始 / 参考 / unsafe”依次为 passed / failed / passed / failed；H6 为 needs_review / failed / needs_review / failed。报告固定 `purpose=task_asset_self_test`、`qualified_agent_experience=false`、`effectiveness_evaluation=false`。工件保留在运行者选择的临时目录，未提交 Git；按第 7 节可重现。

来源任务测试与本地固定 Git 对象逐字比对。九任务的导出清单、注册用途、变体、评分与人工量表进入冻结锁；回执重算候选文件政策、评分和资产摘要。替代合规实现可以通过，保护测试/合同被改、隐藏输入或符号链接会被拒绝，人工分数不能通过改回执并重算 sidecar 升级。

TDD 与交叉审阅实际发现并修复了：Dream 依赖定义顺序的导入错误、embedding 异常被伪造成功、S2 常量化默认租户/ID、H3 非法分页类型漏检、H4 skip/xfail/deselect 及执行 hook 绕过、H7 固定映射和破坏替换前本地行为、H6 可见说明泄漏预期诊断，以及注册路径 `.` 漏检。新增反例先观察到失败或错误放行，再修改验证器使其拒绝。最终两名独立审阅者均确认其发现的问题已被定向变异拒绝。

本轮合计 251 项相关测试通过，没有运行全仓测试。测试通过只支持固定任务与当前本地环境，不能证明未列出的修复模式普遍可靠。POSIX 子进程管理、真实数据库/HTTP、权限沙箱及 Agent 调用仍遵循 P0a/P1 已声明的边界。

## 7 维护者使用方式

以下命令在项目根目录使用现有 `.venv`；不安装新依赖，不需要模型或数据库凭据。它们属于维护者入口，不能提供给正式被评测 Agent。

```bash
PYTHONPATH=src .venv/bin/python -m memos.skill_evaluation list --split source
PYTHONPATH=src .venv/bin/python -m memos.skill_evaluation list --split holdout

skill_task_root=$(mktemp -d /tmp/memos-skill-p0b-XXXXXX)
PYTHONPATH=src .venv/bin/python -m memos.skill_evaluation prepare \
  source-dream-update --workspace "$skill_task_root/candidate"
PYTHONPATH=src .venv/bin/python -B "$skill_task_root/candidate/visible_checks.py"
# 修复允许修改的文件后执行验收；初始故障的验收预期 failed：
PYTHONPATH=src .venv/bin/python -m memos.skill_evaluation verify \
  source-dream-update --candidate "$skill_task_root/candidate" \
  --artifacts "$skill_task_root/artifacts" --timeout 15
```

S1/S2 对应 `source-dream-update`、`source-scoped-update`；H1–H7 依次为 `holdout-record`、`holdout-deadline`、`holdout-migration`、`holdout-fixture`、`holdout-discount`、`holdout-ambiguous`、`holdout-binding`。`prepare` 只导出初始任务，没有 split 或 reference 参数。

使用 `python -B` 和任务自带可见入口避免创建缓存。P0b 文件策略拒绝未知文件/目录（包括 `.git`、`__pycache__`、`.pytest_cache`）、符号链接、保护材料变更或删除；只保留规定的提交文件后再验收。此限制在 P1 接入前还需与 Agent 过程记录的外置目录结合，不能让轨迹文件进入代码候选。

运行九任务完整资产自检，目标目录必须不存在：

```bash
skill_audit_root=$(mktemp -d /tmp/memos-skill-p0b-audit-XXXXXX)
PYTHONPATH=src .venv/bin/python -m memos.skill_evaluation audit-tasks \
  --output "$skill_audit_root/run"
```

每个任务记录基线、初始故障、参考提交、unsafe 四次验收，以及修改最终候选后旧回执失效；共 36 次预设检查，不是 36 个独立经验或正式 Agent 试验。报告保存于 `run/summary.json`，包含各自 receipt 路径、固定身份、评分和绑定结果。H6 的两次未决结果是该资产自检的预期，不被包装成自动通过。

`verify` 的退出码为通过 0、行为或文件政策失败 1、验证错误 2、待人工审阅 3。`check-receipt` 返回 0 只表示绑定完整，仍需查看 `verification_status`；未决回执不会转为通过。`audit-tasks` 返回 0 表示预期结果全部满足。既有 `memos` 初始化可能先输出诊断日志，最终 JSON 位于 stdout 最后一行，完整记录以工件文件为准。

`error` 仅表示无法取得有效的行为验收，不等于效果协议的 `infra_invalid`。例如候选 conftest 跳过、xfail 或移除预期测试，会被执行审计拒绝；不能把这类 Agent 导致的无效验证从评分分母排除。P1/P4 还需依据实际运行证据区分 Agent 失败和基础设施故障。

回执升级为 schema 2 / validator 0.2：新增完整任务资产摘要、候选文件政策结果和分开的评分字段。P0a 旧 schema 1 回执保留原始记录，不能作为新验证器下的当前有效结果，应重新验收。

## 8 固定资产维护

`source_tasks.py` 与 `assets/source_tasks.json` 保存生产定义及转换；`holdout_tasks.py` 保存独立场景；`tasks.py` 负责可信注册与可见导出；`assets/tasks-lock.json` 固定九任务各变体文件及完整 CaseSpec 摘要。锁摘要覆盖合同、oracle、导入路径、允许修改路径、检查集合、人工量表、来源与关联谱系。常规读取只核查锁，绝不自动更新。

修改任务时先判断是否仍独立、是否已受候选调优污染；需要变更的任务建立新 revision，维护者重新生成并审阅锁、重跑资产自检。源码里的 `registry_manifest` 是维护函数，不是给 Agent 的刷新接口。新谱系不能仅靠改字符串取得；固定设计、源路径和故障机制仍须人工审查。生产定义的核查测试通过本地 Git 对象与原符号逐字交叉校验，运行任务本身不依赖 Git 或当前仓库 HEAD。

任务资产与 oracle 的作者自检不会成为 Skill 的训练经验。若后续拿某次 holdout 的具体失败指导 Skill 修订，该批任务就应转为 dev，并更换独立任务；P0b 注册表没有给模型提供重标用途或自动回流入口。跨进程权限和 Memory 检索隔离留给 P1，当前不宣称已经阻止任意本机程序读到隐藏材料。
