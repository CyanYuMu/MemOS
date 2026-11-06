-- 项目邀请：通用链接、单次使用、可过期、可撤销。库里只存 token 的 SHA-256。
CREATE TABLE IF NOT EXISTS project_invites (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    role TEXT NOT NULL,
    token_hash BLOB NOT NULL UNIQUE,
    created_by_account_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by_account_id TEXT NOT NULL DEFAULT '',
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_invites_tenant ON project_invites(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invites_expires ON project_invites(expires_at);
