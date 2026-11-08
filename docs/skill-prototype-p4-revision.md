# P4 来源修订生成配置补充

日期：2026-11-09。范围仅为候选开发生成，不改变正式三组任务预算、来源或候选适用范围。

## 事实与配置变更

修订开发调用 attempt-001、attempt-002 均在 `max_tokens=16000` 时返回 `finish_reason=length`；attempt-003 为 `generation_provider_failed`，没有获得响应模型或用量。失败记录保留，不把截断 JSON 或缺失响应封装成候选，也不读取或保存私有推理。

DeepSeek 官方文档说明 `max_tokens` 限制生成长度，并列出 thinking 模式与输出预算；`length` 也可能表示上下文限制。当前兼容服务的行为只能由实际响应确认，不能仅据文档断言这三次失败的具体根因。[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)

新增显式 `budget_profile="revision_32k_v1"`，仅在带有效 `revision_context` 的来源修订请求中使用，最大 32000 token。默认调用继续保留 16000 上限和旧元数据字段；未知 profile、没有修订上下文、超限和布尔数值均拒绝。timeout、无工具、零重试、固定模型与响应最大字节限制不放宽。

profile 写入 generation 与失败诊断的 parameters，新增 request 摘要绑定模型、prompt/schema 摘要和完整参数。成功候选的 generation 摘要继续绑定全部输入；更大预算不允许复用旧候选身份。只采集可选非负整数 `reasoning_tokens` 用量，不访问 reasoning_content，也不据缺失统计估算推理消耗。

## 验收

在真实 attempt-004 前通过测试：默认上限与旧工件兼容；32k 只用于来源修订；profile/参数/请求摘要篡改拒绝；成功及失败中的推理 token 统计为纯整数，非法值不进入公开报告；私有推理属性永不读取。实际 attempt-004 另存目录，沿用 attempt-002 的 source-only 反馈，最多一次调用，无自动重试。成功后仍须逐项审阅三个问题、引用和范围，保持人工审批未完成。

## 明确标记的来源审阅补丁路径

由于有界模型生成未产出可用结果，新增 `source_review_patch_v1` 作为独立证明类型。P2 父候选仍来自真实 LLM 生成，本次只由开发 Agent 定点修订，`actor_kind=developer_agent`、`human_approved=false`；它不是一次成功的模型生成，也不是人类审阅批准。所有失败保留，正式评估必须在 manifest 中说明候选具有这种开发者辅助修订，不能称为完全自动提炼的收益证明。

入口先完整回读父候选七文件并重算其原始 openai-compatible 生成证明，首版拒绝以另一个 patch 候选为父版本。补丁只允许替换 proposal 的 acceptance、limitations、constraints，以及既有规则的 condition、observation、action、expected_result、branches、evidence；不允许改身份、标题、目标、适用/排除范围、来源、依赖、规则 ID 或 basis。每项补丁绑定一个已有反馈 issue_id 和同一来源 packet 的 fact_id，目标不能重复；修改后仍执行原规则/证据/流程校验。

生成记录保存父 proposal/generation/candidate、精确补丁、反馈上下文、结果 proposal 摘要与操作摘要。封存前后回读来源，父版本与历史 unknown 保留；新版本只作为候选进入验证。具体定点变更为补齐 scoped 回归及 embedding 失败验收与引用、增加合同和当前检查均满足时的无修改出口，以及最多两次修复周期的流程指导。两次周期尚未由运行器逐次自动强制；实际硬预算仍是 240 秒、12 轮、3 USD，文档和候选均须明确这一限制。

## 实际结果

配置变更后的 drafting 回归为 **203 passed、2 skipped**（未配置独立图集成环境），Ruff 与格式检查通过；新增预算、无修订上下文、请求摘要篡改、可选推理计数和非法计数测试均通过。旧无 profile 的生成记录继续通过验证。

attempt-004 使用 `revision_32k_v1`、32000 token 上限、180 秒超时及与 attempt-002 相同的反馈，2.204 秒后得到 `generation_provider_failed`；没有收到 reported_model、finish_reason 或 usage。失败诊断保存明确 profile、参数、原 prompt 摘要和独立 request 摘要，未生成候选，没有自动补跑。本次因此既不能证明实际服务接受 32k，也不能确认此前截断的推理 token 根因；新候选语义审查尚无可用输出。

随后对旧开发模型的安全连接探针观察到 HTTP 429 / `ModelAccountTpmRateLimitExceeded`。经明确选择，attempt-005 改用独立记录的开发生成模型 `qwen3.8-max-0902`，仍使用同一来源、父版本与 attempt-002 反馈，profile 为 `revision_32k_v1`，上限 24000、超时 180 秒；正式 Claude 实验模型不变。该次在 180.938 秒后返回 `generation_provider_failed`，安全传输分类为 `APITimeoutError`，HTTP status 未取得，用量未知。记录保持失败，未自动重试，未产生候选，不声称原模型成功或已确认新模型兼容性。

`source_review_patch_v1` 契约随后通过完整 drafting 回归 **221 passed、2 skipped**，Ruff 与格式检查通过。测试覆盖禁止扩大适用范围、非法目标/引用、父生成证明篡改、修改结果、循环规则、递归 patch 父版本、跨 Skill 身份和伪造人工批准。具体来源补丁由未继承实验规格的独立来源作者制作，本契约实现者未创建或执行实际补丁；实际候选与后续验收由总 P4 文档记录。
