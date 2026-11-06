-- 会话按过期时间清理、审计按工具过滤，都是既有查询的热路径。
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_account_live ON sessions(account_id, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_tool_ts ON audit_records(tenant_id, tool, ts);
