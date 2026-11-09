# 交付执行记录

本记录接续 [总合同](skill-prototype-delivery.md)，只记录实际完成和预登记事项，不把实现计划写成验收结果。

## D1 受托审阅

2026-11-09，经真实服务重新准备固定包后提交 C2 v2 裁决 `e54c3236bbd7a6302a26e88994a6b665bc2492111591903440f590049ddad487`。本次由助手依据用户本轮授权审阅执行，理由中明确并非用户逐项亲审。仅登记 `verify_dream_update#0` 和 `verify_scoped_reads#1`；累计覆盖 4/22，候选仍为 needs_evidence，未发布。

旧 P3 三条不完整记录追加证据不足裁决：`b4d23e1cad72e2fd13fb21d4670e602e3d596e12619e58a4f3d82ca436d814cd`、`cc8d7ad7980757ee3240fc632c48030f91ee0ff83026df9b7466f3a86451e20a`、`5993ceacbe84bb4589f663fa7b0c275d1ab9c926f982c44e02b2b52781b433d2`。其原生捕获及固定加载凭据不完整；原评分保留，resolvedHistory、qualifiedIndependent、repairConfirmed 均为 false，无覆盖贡献。这是完成处置，不是补齐历史证据。

## D3 来源修订 attempt-001 预登记

来源 packet 固定为 `bd696e25dc30abc08af4c0e1424c6236bdb3e62e754f2ef86d23c5dae16b4be5`，父版本为 `f92905541710def3172d16e9c78d7226bf22e646b4c8baa73da499bf826e2a4c`。三条来源、两条 lineage 全部保留；仅一条 Dream 成功经验，不能声称多条独立 Dream 成功证据。

本次用现有 `revise_candidate`，基于来源事实提交收窄范围反馈，最多一次开发生成调用，不自动重试。沿用本机配置的 `deepseek-v4.1-flash-1017`，`revision_32k_v1`，max_tokens=32000，timeout=180 秒；实际请求/响应身份、用量和失败均保留。正式 Claude 独立验证预算另见 D3 子合同，本次开发生成不混入任务成功率。

反馈仅包含来源合同和来源语义审阅：收窄至 Dream UPDATE 多余位置参数调用修复；建议两个节点表达适用判定、健康/最小修复和三种退出；有限周期上限 2 是宿主策略。失败来源不提供根因，scoped 来源只提供背景和范围区别。不得给生成器独立任务输入、隐藏答案或实际验证结果。生成成功后仍须逐条审阅规则与证据，并在真实运行前单独冻结候选和批次。

attempt-001 在 source_precheck 阶段发生 ValueError，没有进入模型生成，无响应或模型用量。记录已保留；不能据此判定来源内容发生变化。随后使用同一来源、作用域、账本及快照独立执行 recheck_packet 成功。另登记 attempt-002：复用相同反馈和生成参数，重新检查来源后最多调用一次模型，继续不自动重试。宿主脚本曾在清理阶段错误调用不存在的 client.close；已删除该清理调用，不修改候选生成器或证据生产代码。

attempt-002 同样在模型调用前失败。对照诊断已定位：宿主全局 umask=077 时旧 receipt 回读报 evidence_receipt_invalid，umask=022 时同一来源通过；快照合同记录目录执行权限，不能由宿主隐式改变。attempt-003 保持全部生成参数和反馈，使用与原验收一致的 umask=022，私有外层目录仍为 0700；最多一次模型调用。两次失败不改写，也不当作模型调用或来源变化。
