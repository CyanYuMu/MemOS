-- 1) 删掉两条被三列索引覆盖的前缀索引。
--
-- 审计分页是 WHERE tenant_id=? ORDER BY ts DESC, id DESC（或 tool=? 跨项目），
-- EXPLAIN QUERY PLAN 显示实际走的是 0020 的 (tenant_id, ts, id) / (tool, ts, id)
-- 覆盖索引；(tenant_id, ts) 与 (tool, ts) 只是它们的前缀，留着只会让每次审计
-- 插入多维护两棵 B-tree。
--
-- 保留：idx_audit_ts（跨租户全局翻页，没有 tenant/tool 条件）、
-- idx_audit_tenant_tool_ts（列序是 tenant → tool → ts，服务于“租户内按工具过滤”）。
DROP INDEX IF EXISTS idx_audit_tenant_ts;
DROP INDEX IF EXISTS idx_audit_tool_ts;

-- 全局翻页（平台管理员看所有项目）的排序键是 (ts DESC, id DESC)，而 idx_audit_ts
-- 只有 ts，同一秒内的行还要走临时 B-tree 排序。换成 (ts, id) 后既是覆盖索引，
-- 清理用的 WHERE ts < ? 也照样能走（最左列范围扫描）。索引总数因此净减两条。
DROP INDEX IF EXISTS idx_audit_ts;
CREATE INDEX IF NOT EXISTS idx_audit_ts_id ON audit_records(ts, id);

-- 2) “没有值”统一成空串，不再混用 NULL。
--
-- 同一列里既有 NULL 又有空串时，每个查询都得写 (x IS NULL OR x = '')；
-- 漏掉一半就是少行，而且这种漏不会报错。这里把可空时间列里的 NULL 归一成空串，
-- 代码侧从此只写、只判 ''（见 store.optionalTimeText / decodeOptionalTime）。
--
-- audit_records.case_like 不在其中：它是有第三态的布尔（NULL = 这条记录不参与该检查），
-- 不是“没有值”。
UPDATE tokens SET last_used_at = '' WHERE last_used_at IS NULL;
UPDATE tokens SET revoked_at   = '' WHERE revoked_at   IS NULL;

UPDATE sessions SET last_seen_at = '' WHERE last_seen_at IS NULL;
UPDATE sessions SET revoked_at   = '' WHERE revoked_at   IS NULL;

UPDATE accounts SET last_login_at = '' WHERE last_login_at IS NULL;

UPDATE project_invites SET used_at    = '' WHERE used_at    IS NULL;
UPDATE project_invites SET revoked_at = '' WHERE revoked_at IS NULL;
