# 从全新经验复现候选 Skill

本指南对应 `codex/skill-contract-repro-v1`，无需作者的历史候选、来源包或账本。复用仓库已有受控来源任务，真实调用 Claude，写入并回读自己的 Memory，再生成一个**未发布候选**及来源图。这是新批次，不会复原旧 c7 revision、替代旧 Release 或自动取得其发布资格。

## 1 环境与配置

按照 README/CONTRIBUTING 安装项目依赖。当前执行隔离只验证了 macOS `sandbox-exec`；需要 Python 3.11+、项目 `.venv`、Claude Code **2.1.296 的受信专用副本**。Memory 使用可写的专用 cube/user，Skill Graph 使用与 Memory 不同的 Neo4j 数据库实例。浏览器、Go 和前端不是来源采集/候选生成的前置条件。

将 [配置模板](../examples/skill_prototype/contract-repro-v1/config.example.json) 复制到仓库外，替换七个配置项。每次使用新的 projectId 和输出目录。模板不接受 API Key、命令或任意来源列表。连接配置从环境变量读取，填入自己的部署值，不要把真实配置提交到 Git：

| 阶段 | 环境变量 |
| --- | --- |
| Claude 来源采集 | `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` 三选一；自定义端点可设 `ANTHROPIC_BASE_URL` |
| Memory | `MEMOS_SKILL_API_URL`；需要认证时设置 `MEMOS_SKILL_API_TOKEN` |
| 候选生成 | `MEMOS_SKILL_LLM_BASE_URL`、`MEMOS_SKILL_LLM_API_KEY`、`MEMOS_SKILL_LLM_MODEL`；使用已有 OpenAI 兼容 JSON 生成器 |
| Skill Graph | `MEMOS_SKILL_GRAPH_URI`、`MEMOS_SKILL_GRAPH_USER`、`MEMOS_SKILL_GRAPH_PASSWORD`、`MEMOS_SKILL_GRAPH_DATABASE`、`MEMOS_SKILL_GRAPH_MEMORY_DB_ID` |

最后一项为 Memory 数据库的真实身份，在该数据库执行 `CALL db.info() YIELD id RETURN id` 查询；不是数据库名称，也不能随意填一个不同值来绕开隔离保护。Skill 图导入会拒绝目标数据库身份与 Memory 相同的情况。

以下变量只是示例位置，使用自己可写的仓库外目录：

```sh
skill_demo_config=/private/config/skill-demo.json
skill_demo_root=/private/runs/skill-demo-001
.venv/bin/python -m memos.skill_demo doctor --config "$skill_demo_config"
.venv/bin/python -m memos.skill_demo selfcheck --output "$skill_demo_root-selfcheck"
```

doctor 检查本地依赖、隔离支持、固定 CLI 版本以及变量是否配置；不会连接模型、Memory 或数据库，`ready` 不代表远端连接已成功。selfcheck 不调用模型：实际验证两个来源任务的健康/故障状态及两个 transfer 任务共十种正确/错误补丁；不能将参考补丁自检当作 Agent 经验。

## 2 分阶段运行

```sh
.venv/bin/python -m memos.skill_demo init --config "$skill_demo_config" --output "$skill_demo_root"
.venv/bin/python -m memos.skill_demo collect --config "$skill_demo_config" --output "$skill_demo_root"
.venv/bin/python -m memos.skill_demo ingest --config "$skill_demo_config" --output "$skill_demo_root"
.venv/bin/python -m memos.skill_demo draft --config "$skill_demo_config" --output "$skill_demo_root"
.venv/bin/python -m memos.skill_demo graph --config "$skill_demo_config" --output "$skill_demo_root"
```

每一步返回 0 再继续下一步。collect 固定调用两个来源任务，各最多 20 轮、300 秒、2 USD；draft 最多一次生成调用、180 秒、16,000 输出 token，费用由提供方决定。模型不保证成功，完整失败会被保留。不要与其他模型实验同时运行以比较耗时；此入口尚未纳入管理服务的执行槽。

| 阶段 | 可检查的工件及边界 |
| --- | --- |
| init | `manifest.json`：固定任务摘要、配置身份和调用预算；已有目录拒绝覆盖 |
| collect | `sources/<case>/collection.json`、原生事件、最终补丁与封存证据；每个新目录只允许一次，两条来源不筛选成功后再纳入 |
| ingest | `memory-ledger.sqlite`、`snapshots/`、`readback-*.json`、`source-plan.json`；完整集合必须实际回读，两个谱系且至少一个合格成功才进入提炼 |
| draft | `draft/job.json`、来源 packet、模型响应及 `draft/candidate/`；结构检查和来源复验失败则保留失败，不换模型或偷偷重试 |
| graph | `graph-*.json`：候选身份、每条规则的来源追溯；图中仍是 candidate，语义审阅 pending |

Dream 和 scoped 两个来源来自已有固定场景。scoped 来源只能支持其自身事实或适用边界，不能算作第二条 Dream 修复成功经验。生成器产出的范围和规则仍需逐条审阅，不保证天然等于已发布的 Dream 最小过程。

Memory 操作结果不确定时，可再次运行 **ingest**：已有账本沿原操作核对，不重新 POST 未知请求。端点身份首次使用即绑定；换端点会拒绝继续。collect 或 draft 已开始后再次运行会拒绝；确需新尝试使用新目录并保留原失败。原生工件属于宿主可信证据，不能接受外部 Agent 自报 JSON 替代。

## 3 候选之后的发布和演进

上述五步自动完成到候选与来源图为止。继续交付时：

1. 根据 `draft/candidate/` 中的规则、引用和限制完成语义审阅。范围不支持时使用已有 source-bound revision 流程生成继任候选，保留父版本。不能把 scoped 经验或一次成功包装为通用能力。
2. 按 [P3 配置](skill-prototype-p3.md) 注册独立图项目和候选目录，启动 Python/Go/前端，在审阅界面查看来源与规则。新 projectId 需要在控制面拥有对应项目及权限；本 CLI 不创建登录账号。
3. 按 [P5 资格协议](skill-prototype-p5-minimal.md) 冻结任务、导出和新分支计划。计划必须引用**新候选的真实规则和 revision**，不能复制旧 c7 的覆盖或 Release。当前规则不兼容 P5 任务时，先修订或明确需要新的执行协议。
4. 独立验证、逐分支审阅、发布事务及正常 Claude 加载按 [操作手册](skill-prototype-operations.md) 执行；来源/工具失效和恢复使用新绑定之后的新执行证据。

这是一条可检查的分阶段流程，尚不是任意模型生成候选都能一键自动发布的向导。既有发布闭环的真实结果见 [交付入口](skill-prototype-handoff.md)；本轮新生成候选的结果单独报告。

## 4 查询验收映射与审阅历史实验

```sh
.venv/bin/python -m memos.skill_contracts catalog --case p5-update-repair-r1
.venv/bin/python -m memos.skill_contracts catalog --case transfer-batch-update
.venv/bin/python -m memos.skill_contracts audit-transfer \
  --batch "$skill_retained_transfer_batch" --output "$skill_new_audit_directory"
```

audit-transfer 只读原批次，回读六次报告、原生检查交付、最终补丁和收据；输出不能位于旧批次内部，不执行模型或验收程序，也不写管理图。完整工件缺失、摘要变化或收据不匹配时拒绝审计。目录内哈希用于宿主工件绑定，不是第三方签名；操作者仍须保管原始批次。

公开的 [审计结果](../examples/skill_prototype/contract-repro-v1/transfer-audit.json) 与 [受托审阅](../examples/skill_prototype/contract-repro-v1/semantic-review.json) 仅追加解释，不改写原评分、不计分支覆盖、不扩大发布范围。检查契约逐项包含独立后端调用、fake 拒绝非法参数、实际 UPDATE、其他动作语义、无目标不写、无内容保留正文、不合资格不写。P5 的完整公开检查清单与全部语义输入也有区别：无内容及不合资格的细部输入由隐藏验收补证，不能说 Agent 亲自执行过。

transfer 的 `update_contract` 只对应实际 UPDATE；隐藏的拒绝检查另支持 fake 合同，其余缺口保留。新任务在 None、空字符串、其他动作上与原 Dream 行为不同，不是换个检查名称就等价。要获得完整资格，需收窄并重新发布一个明确的新过程，或使用满足原合同的任务；本轮不改写历史 Skill 或任务。
