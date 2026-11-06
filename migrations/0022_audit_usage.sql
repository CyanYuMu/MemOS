-- 模型 token 用量。旧网关把它写在 add-bg-*.jsonl 的完成行里，控制台只在
-- 日志详情展示；控制面把同一份 usage 落到异步 add 的终态行，历史数据迁移时
-- 也从 add-bg 补齐。0 表示上游没有回报用量，不是“用了 0 个 token”。
ALTER TABLE audit_records ADD COLUMN request_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audit_records ADD COLUMN response_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audit_records ADD COLUMN llm_calls INTEGER NOT NULL DEFAULT 0;
