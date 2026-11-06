-- 项目软删除：archived_at 非空表示已归档。
--
-- 归档同时把 disabled 置 1，复用现有的“停用项目不可访问”路径（主体角色、项目
-- API Key、邀请兑换都拒绝），而不是在每个鉴权点再补一次 archived 判断。
-- 恢复时把 archived_at 清空、disabled 置 0，回到归档前的可用状态。
ALTER TABLE tenants ADD COLUMN archived_at TEXT NOT NULL DEFAULT '';
