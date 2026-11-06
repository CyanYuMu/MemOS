-- 同一租户串行认领：ClaimJob 需要按 status 过滤并判断该租户是否已有 running 任务。
CREATE INDEX IF NOT EXISTS idx_jobs_status_tenant ON jobs(status, tenant_id);
