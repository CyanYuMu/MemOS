-- 审计按工具过滤：平台管理员跨项目只看某个 tool 时没有 tenant 条件，
-- 既有的 (tenant_id, ts) 与 (tenant_id, tool, ts) 都用不上，会全表扫描。
CREATE INDEX IF NOT EXISTS idx_audit_tool_ts ON audit_records(tool, ts);

-- ClaimJob 按 (status, next_attempt_at) 过滤可认领任务，再按 created_at 排序。
-- 既有的 jobs(status, next_attempt_at) 还要回表排序，补上 created_at 让过滤与排序
-- 走同一个复合索引。
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, next_attempt_at, created_at);

-- tenants.user_id 直接映射上游 user_name；两个项目指到同一个上游用户会共享记忆数据。
-- 空串（历史/占位行）不参与约束。
--
-- 如果历史数据已经存在重复的非空 user_id，本迁移会失败，store.Open 返回错误、
-- 进程拒绝启动。这是有意的“失败要响”：先人工合并重复项目、只保留一个 user_id，
-- 再重启服务。不要为了绕过启动失败而删除或弱化这个索引。
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_user_id ON tenants(user_id) WHERE user_id <> '';
