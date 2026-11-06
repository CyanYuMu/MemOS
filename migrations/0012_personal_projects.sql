-- 个人项目：kind 区分 team/personal，created_by_account_id 记录创建者。
-- 历史行一律视为 team，且没有创建者；个人项目靠部分唯一索引保证一人一个。
ALTER TABLE tenants ADD COLUMN kind TEXT NOT NULL DEFAULT 'team';
ALTER TABLE tenants ADD COLUMN created_by_account_id TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_personal_owner
    ON tenants(created_by_account_id) WHERE kind = 'personal';
