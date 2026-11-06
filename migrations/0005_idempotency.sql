-- 写入幂等：同一租户下同一个键只执行一次，成功结果可重放。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    tool TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    response TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idem_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_updated ON idempotency_keys(updated_at);
