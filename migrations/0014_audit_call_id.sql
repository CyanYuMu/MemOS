-- 控制台按 call_id 合并异步 add 的后台终态行：派生状态的标量子查询与 log/detail
-- 都按 (tool, call_id) 找终态行，没有索引时每行一次全表扫描。
CREATE INDEX IF NOT EXISTS idx_audit_call_tool ON audit_records(call_id, tool);
