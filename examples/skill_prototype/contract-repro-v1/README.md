# 验收契约与公开复现工件

- `config.example.json`：无凭据的复现配置模板，复制到仓库外填写。
- `transfer-audit.json`：只读回读原六单元批次后的结果，绑定原报告、原生事件、最终快照及收据；不是发布证据。
- `semantic-review.json`：用户授权后的助手受托审阅，区分正确补丁、受支持动作和未被支持的完整 Skill success 路径。它是追加的离线记录，不是管理图中的新裁决或用户逐项亲审。
- `transfer-audit-final.json`、`semantic-review-final.json`：最终追加版本，明确无 Skill 的对照单元无需规则审阅；两个 Skill 的实质判断不变，前版文件继续保留。
- `fresh-demo-summary.json`：两条全新真实来源、Memory 回读、一次提炼、六规则来源图和幂等/重跑保护的公开验收摘要。

操作步骤见 [公开复现指南](../../../docs/skill-prototype-public-reproduction.md)，检查义务和实现摘要在 [catalog.json](../../../src/memos/skill_contracts/catalog.json)。哈希建立工件关联，不能替代完整证据保管或第三方签名。

两次历史 Skill 运行的最小编辑正确，模型也明确没有执行隐藏检查。但公开检查只证明单项 UPDATE，新任务与原 Skill 在其他动作、None 和空内容语义上不等价，因此不计完整规则使用及分支覆盖。六次原评分、原 `unreviewed` 字段和已发布版本全部保留。
