-- 审计行补来源：request_id 用于与同一请求的日志对齐，remote_ip 是 TCP 对端地址。
-- 老数据留空串（不可回溯），新行由 transport 在写审计前填。
ALTER TABLE audit_records ADD COLUMN request_id TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_records ADD COLUMN remote_ip TEXT NOT NULL DEFAULT '';
