# Skill 本机可用性优化：操作与验收

日期：2026-11-11。对应 [开发计划](skill-prototype-usability-v1.md)，补充 [原型交付入口](skill-prototype-handoff.md)。本轮代码仅在本机三个优化分支提交，未推送或更新原 PR。原 P4/P5 批次、Skill 内容、Release 和来源记录不改写。

## 1 已完成的产品改进

| 工作包 | 本轮行为 | 实际验收 |
| --- | --- | --- |
| U1 监测补齐 | 为既有暂停版本追加不可变监测绑定，覆盖 `skill_management` 和 `skill_lifecycle` 当前 Python 文件；旧主体及预期摘要保留。新 P5 候选初次登记直接覆盖这些文件 | 旧验证不能恢复升级后的暂停；新增监测项扫描一致，升级后的真实两单元恢复批次通过，受托审阅后恢复 normal 准入 |
| U2 常驻服务正常运行 | 新 `runs/start` 入口由服务器固定 normal 模式；网页支持正式运行，默认选择当前 Release 范围内的开发任务 | 浏览器启动真实 Claude 作业 `27c7578804f6…`，无需停止管理服务，最终 passed，加载、采集、最终补丁及结束准入均核验 |
| U3 未知反馈恢复 | 新 `jobs/reconcile` 管理权限入口重新验读宿主原生工件及图中执行票据，追加独立恢复记录 | 原作业 `9c5ad97cb96e…` 保持 unknown 和原错误；恢复记录 `b20ab7b7d6aa…` 指向已核验验证 `289b9f0bd748…`；网页同屏展示原状态与补回反馈 |
| U4 同合同迁移探测 | 新批量调用和版本检查两个任务实例，三组共六次，不作为新的问题族或发布资格 | 任务自检、固定批次及结果见下节；不改变原 Release 的 caseIds |

当前 Release 仍为 `b23912814a518f795dd4598b3f009e514f779eda9873b8255ff7c81da84e951b`；Skill revision 仍为 `c7fb3c895b2d49edc82c2808d551224d7c5ef705529d966b7973c6c042b3b8d6`。正常作业的反馈验证是 `2c41367430dd886f06553d1d4f380294db5c357a629d39992d52976c14bd9434`，属于 isolated_dev，规则使用仍为 unreviewed，空分支覆盖。

监测绑定从 `24d65e3dd17c433fddee43c67cd5891afb476bf42017b09d04b34b436be37a1d` 追加到 `4d08c8ef8f3201d85109bc8a886361679ec4dbdde459f9e1521fa9b15e2fde98`。升级后管理状态仍 paused；恢复批次 `f70de5c450435156f31868e089bdb528189e31804b211954e9fb0e2eb2731ecd` 的 none 和 skill 两单元均通过。受托审阅只确认 `repair_and_verify_update#0`，不追授缺少目标 select 声明的入口覆盖；恢复使用该批次的新执行票据及验证，而非重新导入旧证据。

## 2 接口与使用

保持既有 Memory、独立 Skill Neo4j、Python Skill 服务、Go 控制面和前端配置，使用 [P3 启动器](../scripts/dev/skill_p3_console.py)。凭据和部署路径仍保存在仓库外的私有 JSON 中。部署需三个配套分支；旧 Python 读取器不认识新增的 `feedback_recovery` 内部记录，不能在已写入新记录后直接降级读取器。监测迁移需短时停止管理服务以取得独占锁；日常任务和反馈恢复均可通过常驻服务完成。

| Go POST 路径 | 请求字段 | 权限及约束 |
| --- | --- | --- |
| `/api/v1/skills/runs/start` | projectId、skillId、revision、caseId、operationId | write；不能传 mode、命令或路径。启动时重新检查发布和来源/工具准入；与隔离评测共用执行槽 |
| `/api/v1/skills/jobs/detail` | projectId、jobId | view；查询原状态和可选 feedbackRecovery |
| `/api/v1/skills/jobs/reconcile` | projectId、jobId、operationId | manage；只处理 unknown；不接受报告、文件路径或候选验证，不调用模型 |

开发环境先在 Go 登录页 `http://127.0.0.1:5280/__login` 使用私有配置中的既有账号登录，再打开 `http://127.0.0.1:5274/cn/skills` 并选择审阅项目；前端开发服务器本身不提供登录页。本轮浏览器重启后的旧会话失效已通过该入口重新登录。

网页操作：选择已发布版本 → 选择服务器许可任务 → “使用已发布版本运行” → 查看轮询结果。“检查 normal 加载准入”仅是预览，不能替代启动校验。任务完成后，“历史运行”按创建时间倒序排列。操作请求不会自动重试；网络错误后先重新读取记录再决定下一步。

历史恢复：展开历史运行 → 在 unknown 作业点击“核验并补回反馈”。恢复同时核对原 job 身份、候选/导出摘要、原生报告、事件、图中 begin/finish 票据和最终快照/回执。保留原 unknown，追加新的 feedbackRecovery；不是把历史作业改成 passed。实际对工件不完整的 `335d738194f7…` 作业调用恢复，得到 422 且原作业逐字段不变。缺失工件、未完成票据、篡改证据或不支持的协议返回 `feedback_unverified`；不会通过重新调用 Claude 填补事实。相同恢复可幂等重放，正常反馈不支持独立发布覆盖。

监测维护使用 [宿主脚本](../scripts/dev/skill_monitor_upgrade.py)。先在控制台暂停目标版本，读取当前 sequence 和 bindingId，再停止管理服务：

```sh
.venv/bin/python scripts/dev/skill_monitor_upgrade.py \
  --config "$skill_private_config" --project "$skill_project" \
  --skill "$skill_id" --revision "$skill_revision" \
  --binding "$skill_binding" --sequence "$skill_sequence" \
  --operation maintenance-001 --reason '追加正常执行代码监测'
```

重新启动服务并扫描，按既有 P5 recovery 协议执行新批次、回读事实、审阅并 restore。只有当前观察一致、升级后真实执行且被裁决合格的验证能恢复。`extend_lifecycle` 只追加主体；不能替换旧基线来把工具变化清零。

## 3 新任务的复现与结果

`skill_transfer` 是单独的实验目录，不注册为 normal 任务，也不写入来源 Memory。为保留旧执行器摘要，CLI 编排从冻结的 P4.1 流程建立新版本，继续复用原生 hooks、OS 隔离、快照和有资源限制的验证进程；历史 runner 字节保持不变。该取舍暂时保留了编排重复，之后可在新的协议版本中抽取可配置任务目录，不能就地改写历史协议。

复现前准备原固定候选目录及私有配置；使用受信 CLI 专用副本。先完成自检，再冻结，最后运行，输出目录不得复用：

```sh
.venv/bin/python -m memos.skill_transfer qualify --output "$skill_run_root/qualification"
.venv/bin/python -m memos.skill_transfer freeze \
  --config "$skill_private_config" --candidate "$skill_candidate" \
  --qualification "$skill_run_root/qualification" --output "$skill_run_root/batch"
.venv/bin/python -m memos.skill_transfer run \
  --config "$skill_private_config" --candidate "$skill_candidate" \
  --qualification "$skill_run_root/qualification" --output "$skill_run_root/batch"
```

`started.json` 使用独占创建，部分失败或中断后再次 run 会拒绝，防止悄悄重试到成功。需要新运行时建立新批次并保留旧批次。实际试验期间不并行运行其他模型任务，以免干扰耗时；此 CLI 尚不通过管理服务执行槽调度。

本轮冻结批次 `6184b13fdfd7630317c02d61753b022d6b4ab48573328dab0c39773c416d9caa`，Claude Code 2.1.296，模型 `claude-opus-5-5`，effort low，每单元 20 轮、240 秒、3 USD，六次最多模型执行。原始经验组按固定 source/fact 排序纳入 59 条完整来源事实，共 32,524 字节，另 2 条因容量限制省略；仅对私有工件路径脱敏，不重新总结成解题指南。这是固定来源事实投影，并非 Memory 全量导出。

[公共结果摘要](../examples/skill_prototype/usability-v1/results.json)保留冻结清单、六个报告摘要和功能验收身份。六个单元的报告/事件摘要、最终补丁收据与评分已再次回读核对，全部行为通过、采集完整，两个 Skill 单元加载核验通过，两个原始经验单元均观察到读取。

| 实验组 | 行为通过 | 总耗时（秒） | 总报告费用（USD） |
| --- | --- | --- | --- |
| 无经验 | 2/2 | 95.623 | 0.1023762 |
| 原始来源事实 | 2/2 | 131.225 | 0.3036052 |
| 固定 Skill | 2/2 | 113.825 | 0.2118250 |

这两个容易定位的缺陷实例上，无经验组已达 2/2，未观察到成功率提升。Skill 组总耗时和费用高于无经验组、低于原始来源事实组；两个样本不能支持统计推断或因果收益结论。六次没有模型行为失败；负例来自任务自检的错误修复/弱化检查，历史 P4 的真实失败和未知仍在原报告中，不能混作本批次结果。

行为通过、采集/加载与规则使用判断分开。新任务的公开检查名为 `update_contract`，不同于原发布任务的五项 Dream 检查清单。两个 Skill 组均只声明了实际可见检查，并未声称执行隐藏验收；但它们选择 success 是否符合原规则的完整验收要求仍未裁决。因此，即使最终补丁通过，也不能认定新任务已获得原 Skill 的完整使用资格或扩展现有发布范围。

## 4 测试与局限

| 检查 | 结果 |
| --- | --- |
| Python 管理、生命周期、P5 完整定向回归 | 466 passed；包含真实独立 Neo4j 测试及 OS 隔离验证 |
| 监测升级与配置补充检查 | 16 passed，和上述回归有重叠；新增用例确认旧证据不能恢复、升级后新执行可以恢复 |
| 新迁移任务、工件篡改、公开检查代理与评分边界 | 19 passed |
| Go `make check` | lint、vet、race/shuffle 测试、文档检查和构建全部通过；后续 OpenAPI 补充的定向契约检查也通过 |
| 前端 `pnpm verify` | 262 项 Vitest、类型、ESLint、78 项附加检查及构建通过；保留已有大分块构建提示 |
| 浏览器验收 | 已展示已发布任务的实际启动/通过，以及原 unknown 与恢复反馈同屏显示 |

这仍是 macOS 本机、单宿主原型。未增加分布式执行租约、Linux/Windows 沙箱、多 Coding Agent 适配或通用自动提炼。变化检测发生在准入/执行边界和显式扫描时，不是持续后台监听。恢复当前只支持保留了完整宿主结果的 `p5-normal-2` 工件；无法补回不存在的信息。

两道新题属于同一已知缺陷族，开发者知晓 Skill 和判据；每组每题仅一次。费用是提供方原生报告值，耗时包含本机编排开销，不是账单结算或因果收益证明。Skill 规则使用仍待具体任务的语义审阅，不增加分支覆盖。更大样本、检查契约映射、不同问题族以及多 Agent 适配属于后续工作；本轮不为补齐它们而扩大交付范围。
