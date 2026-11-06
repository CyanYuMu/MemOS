-- 审计可诊断：记录真实的 HTTP 状态与调用方可读的错误消息。
--
-- 之前 log/detail 只能用 ok 推导 200/500，403/409/503 在页面上全变成 500；
-- err 列只存错误类型名，运维看不到“为什么失败”。两列都只增不改，历史行留空/0。
ALTER TABLE audit_records ADD COLUMN err_message TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_records ADD COLUMN http_status INTEGER NOT NULL DEFAULT 0;
