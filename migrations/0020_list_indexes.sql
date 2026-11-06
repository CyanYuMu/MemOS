-- 列表分页的排序键是 (ts DESC, id DESC)，既有索引只到 ts，相同时间戳的
-- 多行仍需回表排序；补上 id 让租户内/工具维度的分页都走覆盖排序。
CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts_id ON audit_records(tenant_id, ts, id);
CREATE INDEX IF NOT EXISTS idx_audit_tool_ts_id ON audit_records(tool, ts, id);

-- 账号列表按 created_at 排序分页。
CREATE INDEX IF NOT EXISTS idx_accounts_created ON accounts(created_at, id);

-- 管理端按项目排查任务时按 tenant_id 过滤；现有索引都以 status 开头，用不上。
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created ON jobs(tenant_id, created_at);
