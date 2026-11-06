-- 当前项目空间在账号上保存一份：零信任等外部身份不建会话，
-- 只能把“当前项目”跟着账号走，否则多项目用户切不了项目。
ALTER TABLE accounts ADD COLUMN current_tenant_id TEXT NOT NULL DEFAULT '';
