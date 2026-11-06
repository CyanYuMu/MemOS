-- next_attempt_at：重试退避的下次可认领时间。老行回填 created_at，语义仍是“立即可认领”。
ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT '';
UPDATE jobs SET next_attempt_at = created_at WHERE next_attempt_at = '';

-- 认领时按 (status, next_attempt_at) 过滤到期的 pending 任务。
CREATE INDEX IF NOT EXISTS idx_jobs_status_next ON jobs(status, next_attempt_at);
