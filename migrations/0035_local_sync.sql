-- 本地插件记忆镜像：设备授权码、设备令牌、上传批次/分块与镜像行。
--
-- 控制面只保存客户端上传来源的副本，用于展示、审计与显式转存；检索仍归 memos-api。
-- 时间列沿用固定宽度毫秒 UTC 文本（见 store.md），布尔用 INTEGER。

CREATE TABLE IF NOT EXISTS local_device_codes (
    code_hash    BLOB PRIMARY KEY,
    user_code    TEXT NOT NULL UNIQUE,
    device_label TEXT NOT NULL DEFAULT '',
    agent_kind   TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    account_id   TEXT NOT NULL DEFAULT '',
    tenant_id    TEXT NOT NULL DEFAULT '',
    expires_at   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_codes_expires ON local_device_codes(expires_at);

CREATE TABLE IF NOT EXISTS local_devices (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    agent_kind   TEXT NOT NULL DEFAULT '',
    profile_id   TEXT NOT NULL DEFAULT '',
    token_hash   BLOB NOT NULL UNIQUE,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_devices_tenant_device
    ON local_devices(tenant_id, device_id);
CREATE INDEX IF NOT EXISTS idx_local_devices_tenant
    ON local_devices(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS local_sync_batches (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    device_id      TEXT NOT NULL,
    status         TEXT NOT NULL,
    layers         TEXT NOT NULL,
    manifest_json  TEXT NOT NULL DEFAULT '{}',
    manifest_hash  TEXT NOT NULL DEFAULT '',
    trace_mode     TEXT NOT NULL DEFAULT 'full',
    received_bytes INTEGER NOT NULL DEFAULT 0,
    accepted       INTEGER NOT NULL DEFAULT 0,
    rejected       INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    result_json    TEXT NOT NULL DEFAULT '',
    cursor_at      TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    committed_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_batches_tenant_device_id
    ON local_sync_batches(tenant_id, device_id, id);
CREATE INDEX IF NOT EXISTS idx_local_batches_status_created
    ON local_sync_batches(status, created_at);

CREATE TABLE IF NOT EXISTS local_sync_chunks (
    batch_id       TEXT NOT NULL REFERENCES local_sync_batches(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    body           BLOB NOT NULL,
    received_bytes INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);

CREATE TABLE IF NOT EXISTS local_memories (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    device_id          TEXT NOT NULL,
    layer              TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    payload            BLOB NOT NULL,
    source_updated_at  TEXT NOT NULL,
    deleted_at         TEXT,
    promoted_memory_id TEXT NOT NULL DEFAULT '',
    promote_job_id     TEXT NOT NULL DEFAULT '',
    promote_status     TEXT NOT NULL DEFAULT '',
    promote_error      TEXT NOT NULL DEFAULT '',
    synced_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_memories_unique
    ON local_memories(tenant_id, device_id, layer, source_id);
CREATE INDEX IF NOT EXISTS idx_local_memories_layer_time
    ON local_memories(tenant_id, layer, source_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_local_memories_device
    ON local_memories(tenant_id, device_id);
CREATE INDEX IF NOT EXISTS idx_local_memories_promote
    ON local_memories(tenant_id, promote_status);
