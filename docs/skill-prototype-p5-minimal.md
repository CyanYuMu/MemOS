# P5 最小闭环开发与验收协议

本计划接续已保留的 P4、release-batch-001 和 G1 记录。用户要求尽快完成正式发布、正常加载和失效恢复；不增加新业务场景。历史失败及其裁决不改写，不自动转换成新版覆盖。

## 范围与交付顺序

1. 以真实回读的三条 MemorySnapshot 和现有 Dream UPDATE 候选为父版本，使用已有 `source_review_patch_v1` 生成有明确变更依据的新版本。只支持受控 Dream UPDATE 调用中删除多余位置参数；来源不能支持的异常诊断、重试次数和通用 mock/fixture 能力不得声称已验证。执行声明、停止条件属于宿主规范，不伪装成经验事实。
2. 建立独立 `skill_p5` 执行协议。冻结的 `skill_release`、旧适配器及历史任务保持原字节。复用其成熟流程并保留版本边界，修复 MCP 错误结果以 JSON 字符串交付时的解析；缺合同任务限定为同一 UPDATE 调用合同缺失，不要求新增冲突策略。新协议独立登记生产者、任务摘要和 schema 7，旧 schema 1–6 继续原分派。
3. 固定一次七单元批次：一个无 Skill 校准，加修复、健康、范围外、缺证据、残留失败、工具失败六个 Skill 单元。先做离线任务隔离与反例自检，冻结配置、候选、导出和预算，再调用真实 Claude。每单元上限 240 秒、20 turns、3 美元；声明和检查也占用预算。每单元只运行一次，不为取得覆盖静默重试。不同任务与来源保持独立，结构相近的派生任务不算新增独立问题族。
4. 逐条审阅实际事件、最终补丁及独立验收回执。用户已授权助手受托审阅和执行发布；记录准确标识该身份，不声称用户逐条批准。必须满足来源复核、内容审阅、完整分支/边界覆盖及发布事务门槛才发布。
5. 使用正式 Release 完成一次正常 Claude 加载与修复。对专用 CLI 副本制造可逆不可执行状态，实际扫描确认准入阻断；恢复文件权限后不得自动恢复发布资格，运行固定恢复批次并裁决，执行恢复事务，再正常加载。
6. 浏览器展示候选依据、裁决、Release、准入与生命周期记录，整理脱敏实验摘要、操作步骤和限制。检查 Python、Go、前端相关测试，中文提交后推送并向上游对应分支提交关联 issue #6 的 PR。

## 验收标准

- 历史目录与冻结生产者哈希保持一致；新协议不能假冒旧协议身份。
- MCP 正常失败与工具异常严格区分；JSON 字符串、结构化内容和错误包装均绑定同一实际调用，不虚构检查结果。
- 当前任务没有权威合同必须停止；范围外任务不能引用范围内成功路径；真实声明在其对应动作之前，结果只引用已经发生的检查。
- 独立验收绑定最终快照、任务和验收器摘要；保留发现额外位置参数、空读、跨作用域污染和 MERGE 回归的能力。
- 所有声称覆盖的分支有条件、声明、动作和检查引用；正确补丁不自动证明规则使用正确。失败可阻断发布，不能为了交付降低门槛。
- 发布、撤回/失效、恢复均经项目锁事务，并绑定精确 content/export/revision；普通加载不能使用候选或失效 Release。
- 实际工具变化、受影响节点和准入结果可回读；恢复需要变化之后的新验收证据。

## 实施边界与风险

`skill_p5` 是冻结协议的显式继任版本；复制版本化执行代码会增加维护成本，换取历史事实可复验。后续可提取稳定公共实现，本次不进行大规模重构。Claude 的声明与工具选择仍有不确定性；本次不承诺收益优势，不把一次通过推广成通用技能。既有三组对照和失败案例继续报告，新增批次是发布资格验证。

真实运行结果、发布 ID、成本、耗时和未完成项在执行后补入验收记录；本文件本身不代表已通过验收。

## 可复现入口

使用现有私有服务配置，在新候选登记项增加 `"executionProfile": "p5"`。凭据、来源快照、完整事件和生成目录放在仓库外。`CANDIDATE` 指向可由 `load_candidate` 完整复验的候选目录；`ARTIFACTS` 是全新私有运行目录。示例中的环境变量由操作者按部署设置，不需要把配置提交到 Git。

```sh
.venv/bin/python -m memos.skill_p5 qualify --output "$ARTIFACTS/qualification"
.venv/bin/python -m memos.skill_p5 run --config "$CONFIG" \
  --candidate "$CANDIDATE" --qualification "$ARTIFACTS/qualification" \
  --branch-plan "$BRANCH_PLAN" --output "$ARTIFACTS/batch" --project "$PROJECT"
.venv/bin/python -m memos.skill_p5 import --config "$CONFIG" \
  --batch "$ARTIFACTS/batch" --qualification "$ARTIFACTS/qualification" \
  --allowed-root "$ARTIFACTS" --output "$ARTIFACTS/import" \
  --project "$PROJECT" --skill "$SKILL"
```

分支计划为六个 suite ID 到候选分支 ID 列表的 JSON 映射，必须覆盖候选的所有分支，意向不等于实际覆盖。固定 suite 名称见 `skill_p5.tasks.SUITES`。恢复时在 `run` 增加 `--plan-purpose recovery` 并使用新的输出目录：该计划只运行校准和修复两个单元。

在管理界面逐条准备裁决包，检查实际事件和检查引用，再提交裁决。取得全部覆盖和发布门槛 PASS 后准备固定审阅包并发布；任何缺口都应保留。新协议的 schema 7 在 Python、Go OpenAPI 和前端均有独立身份，旧 schema 6 不变。

正式 normal 使用须先停止管理服务，让宿主独占同一服务锁，然后运行：

```sh
.venv/bin/python scripts/dev/skill_p5_normal.py --config "$CONFIG" \
  --project "$PROJECT" --skill "$SKILL" --revision "$REVISION" \
  --case p5-update-repair-r1 --operation p5-normal-001 \
  --output "$ARTIFACTS/normal-001.json"
```

操作 ID 和输出路径不能重复使用。服务配置若保留旧三组实验，必须继续匹配其冻结运行配置；新批次可使用独立配置文件。normal 命令结束后重启管理服务查看作业和反馈。该任务是已知场景的使用演示，不能增加独立覆盖或被计为新的效果样本。

工具失效演示仅操作部署中专用的 Claude CLI 副本：记录原权限，移除执行权限，调用来源/工具扫描并核对 normal 拒绝；用 `finally` 恢复原权限并再次扫描。此时应仍有 `restored_pending` 根因。完成变化之后的恢复批次、回读和裁决，再审阅固定影响包执行 restore。不要更改用户全局 Claude 安装，也不要把“文件恢复”描述为“已重新获准使用”。
