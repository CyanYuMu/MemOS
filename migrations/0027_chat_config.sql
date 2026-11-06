-- 控制台对话页的 BYOK 模型配置，每个项目一行。
--
-- api_key 是明文：pi 需要原文写进 models.json 并通过环境变量注入，无法只存哈希。
-- 它与 tokens.raw 同属“控制面自有凭据”，只允许项目写权限读写；接口从不回传明文。
CREATE TABLE IF NOT EXISTS chat_configs (
    tenant_id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
