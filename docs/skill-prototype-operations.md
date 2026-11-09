# Skill 原型操作手册

本手册对应当前 `skill_p5` 生命周期与发布验证版本。实际通过项、发布身份及限制以 [P5 结果](skill-prototype-p5-results.md) 和 [交付记录](skill-prototype-delivery-record.md) 为准；命令存在不代表某次实际运行已通过。

2026-11-09 收尾入口见 [交付指南](skill-prototype-handoff.md)。窄范围 Dream UPDATE 候选已正式发布，完成实际工具失效、恢复后独立验证与受托恢复，最终正常 Claude 作业及反馈写回均通过。当前使用 `skill_p5`、schema 7 和 `management-native-3`。现有工作台保留旧合同，但未接入实验性的跨模块 `skill_release_v2`，不要混用它与当前执行协议的工件。

## 1 准备与启动

使用仓库现有 `.venv`、Go 和 pnpm 环境，按各仓库 README、CONTRIBUTING 和 Makefile 完成检查。Python、Go、前端代码分别在三个工作区，运行前确认它们支持相同 API 和当前 schema 7。私有配置放在仓库外，文件权限 0600，工件外层目录 0700。不得把模型密钥、数据库密码、原生轨迹或部署路径写入 Git。

`console.json` 沿用 [P3 启动配置](skill-prototype-p3.md)，其中 `serviceConfig` 指向管理服务配置；`logRoot` 为本次日志目录。工具副本必须是经过版本及字节校验的固定 CLI，不能在运行中更换。启动器不会自动停止占用端口的旧进程，也不安装依赖。

```sh
umask 022
.venv/bin/python scripts/dev/skill_p3_console.py --config /private/console.json --check
.venv/bin/python -u scripts/dev/skill_p3_console.py --config /private/console.json
```

022 用于保持已冻结验收的目录权限语义；私有外层目录与配置仍须保持上述限制。终端按服务标记日志，`logRoot` 保存副本。Ctrl+C 关闭本次启动的 Skill、Go 和前端，保留先前运行的 Memory、Neo4j、Qdrant。端口占用时先核对进程身份，不杀死未知进程。

## 2 检查固定版本与来源

在控制台选择项目和 Skill，核对 revision、contentHash、exportHash、sequence、发布状态和来源水位。规则详情可追溯到 EvidenceRevision 和 MemorySnapshot。原始 unknown、失败和受托裁决分别保留，不能从当前状态反推历史成功。

来源或工具扫描由管理权限调用 `POST /api/v1/skills/lifecycle/scan`。正文为固定 projectId、skillId、revision 加 expectedSequence、operationId、reason；浏览器不能自报观察结果、宿主路径或任意探针命令。相同 operationId 是幂等回读；想获取新观察必须使用新的 operationId。

扫描范围以候选冻结的生命周期绑定清单为准。`skill_management/p5_runtime.py` 的摘要保存在运行 manifest 中，但本次清单未监测该新正常编排器；其变更可追溯，不会自动触发失效。部署者不能据扫描 healthy 推断所有宿主代码均未改变。扩大清单应使用新绑定和重新验证，保留旧基线。

## 3 独立资格与固定批次

资格检查不调用模型。输出目录必须全新，所有运行及失败记录保留。真实运行前，候选须已由管理服务登记，登记项指定 `"executionProfile": "p5"`，绑定固定导出及生命周期主体；执行票据要求当前观察健康。冻结细节见 [最小协议](skill-prototype-p5-minimal.md)。

`branch-plan.json` 是六个 suite ID 到非空分支 ID 数组的映射，例如 `{"update-repair-r1":["check_update_contract#2","repair_and_verify_update#0"], ...}`。六项必须齐全，每项无重复且只引用候选真实分支，全部数组的并集覆盖候选所有分支。recovery 仍保留完整映射，实际只执行校准和修复单元。

```sh
.venv/bin/python -m memos.skill_p5 qualify --output /private/qualification-001
.venv/bin/python -m memos.skill_p5 run \
  --config /private/service.json \
  --project PROJECT_ID \
  --candidate /private/candidate \
  --qualification /private/qualification-001 \
  --branch-plan /private/branch-plan.json \
  --output /private/release-batch-001 --plan-purpose release
```

首次发布计划最多七次模型调用：一次无 Skill 校准和六个固定 Skill 任务。恢复计划使用 `--plan-purpose recovery`，最多两次调用。候选、任务、资格、模型、CLI、预算、代码摘要及分支意图在 manifest 固定。不能修改失败批次或重复同一单元直到通过；需要新运行时先登记原因和新批次。

宿主回收命令如下，它只追加经重新核验的事实，不作语义裁决或发布：

```sh
.venv/bin/python -m memos.skill_p5 import \
  --config /private/service.json --project PROJECT_ID --skill SKILL_ID \
  --batch /private/release-batch-001 \
  --qualification /private/qualification-001 \
  --allowed-root /private --output /private/import-001
```

资格使用派生任务，新增独立来源样本数为零。健康、修复、范围外、缺证据、未解决和工具故障分别呈现；最终测试通过不自动授予规则使用合规或分支覆盖。

## 4 审阅、发布与正常使用

准备当前 assessment 的固定裁决包，逐条核对实际事件、同一补丁的检查、规则声明和动作顺序。实际有修改的入口分支必须有修改前的规则选择证据。勾选分支只是待提交决定，服务器仍重算资格。

准备发布包后检查缺口、来源/工具状态、挑战和固定依赖 release IDs。提交时绑定 packageId、packageHash、expectedSequence、operationId 和具体理由。409 应按 error.code 处理：陈旧包或序号冲突需要重新读取和判断；operation_conflict 表示同一操作 ID 被用于不同请求，应核对原操作；immutable_conflict 或图合同异常必须先排查证据与存储冲突。不能统一换包重试。自动检查通过仍需有权限的审阅者完成语义决定；助手受托执行时必须如实记录授权与审阅主体。

正常加载预览为 `POST /api/v1/skills/load-preview`，mode 固定 normal，版本和 caseId 必须属于实际发布范围。正常执行使用宿主 `SkillService.start(..., mode="normal")`，任务前后均扫描并核对固定准入。HTTP 的候选评测入口不能代替正常执行。已提供 [宿主命令](../scripts/dev/skill_p5_normal.py)，它在同一服务锁内构建服务、启动并等待作业、关闭后释放锁：

```sh
# 先停止管理服务，避免多个进程共享同一运行目录。
.venv/bin/python scripts/dev/skill_p5_normal.py --config /private/service.json \
  --project PROJECT_ID --skill SKILL_ID --revision REVISION \
  --case p5-update-repair-r1 --operation normal-001 \
  --output /private/normal-001.json
```

输出路径和操作 ID 不得复用，结束后重启管理服务查看记录。配置若仍登记旧三组实验，其运行参数必须匹配旧冻结值；新资格批次可以使用独立配置。正常使用回收为 dev/反馈，不产生独立分支覆盖；复用已验收任务时保留原 CaseRevision 的 holdout 身份，当前 Validation 单独记录 dev 角色。

## 5 失效、恢复、撤回和替代

扫描区分 changed、unavailable、invalid、restored。外部条件恢复后根因仍为 restored_pending。固定 REQUIRES 反向传播全部根因，RELATED_TO 不传播；依赖或来源恢复不能自动清除其他根因。

恢复顺序为：先恢复外部条件并扫描为 healthy/restored，再冻结和执行新的 recovery 批次、import 事实、完成当前 schema 7 合格裁决，最后准备生命周期包并提交 restore。主体仍 unavailable 时不会签发执行票据。服务器在执行前签发票据、执行后绑定原生报告和回执摘要；故障发生前执行但恢复后才导入的旧任务不能用于恢复。逐项选择根因和合格新验证，剩余任一根因仍阻断正常加载。暂停亦需重新验证，撤回版本不能恢复。

当前最小故障演示只临时取消专用 Claude CLI 副本的执行权限，并在 `finally` 中恢复原权限和核对字节。不要修改全局安装，也不要为了演示暂停用户的 Memory 或存储服务。真实双根因演示与依赖传播测试的范围见验收结果，不能从单个工具恢复推断全部情形已实测。

历史挑战可追加 addressed、upheld 或 superseded 处置。addressed 必须有对应事实证据；upheld 保留阻断；superseded 必须关联当前健康、真实发布的新版本。新版本替代不证明旧规则正确，也不改变固定依赖的目标。原正文、unknown、批次及裁决始终保留。

## 6 异常处理与证据边界

模型超时、捕获不完整、加载未证明、任务结束准入变化、检查工具错误均保留实际状态。补丁行为可以通过而执行状态仍为 unknown，不将两者合并。恢复不能仅凭文件时间或后来补写的声明。

测试及操作记录应区分真实 Claude 任务、合成事件加真实 OS 检查、隔离 Neo4j 事务测试和浏览器验收。浏览器权限或锁屏阻断时标记前端实测未完成；单元测试不能替代真实页面验收。原型的小样本发布资格不等同于普遍成功率或成本改善。
