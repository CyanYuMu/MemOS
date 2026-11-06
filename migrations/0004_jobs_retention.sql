-- worker 每小时按 (status, updated_at) 清理终态任务。
CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status, updated_at);
