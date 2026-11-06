-- 客户端与宿主元数据。工具名与 source 只回答“谁调了什么、从哪个入口进来的”，
-- 回答不了“哪一版插件、哪一版宿主、哪个部署身份、是不是子 agent、第几轮”。
-- 这五项都由插件在请求头里自报，网关只做解析与截断，不参与鉴权。
-- 历史行与 MCP 入口为空：前者没有这些头，后者不是插件发的。
ALTER TABLE audit_records ADD COLUMN client_version TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_records ADD COLUMN host_version TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_records ADD COLUMN agent_identity TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_records ADD COLUMN agent_context TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_records ADD COLUMN turn_number INTEGER NOT NULL DEFAULT 0;
