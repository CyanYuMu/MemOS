-- 时间列统一成固定宽度毫秒 UTC：2026-10-23T00:00:00.000Z。
--
-- 旧的 encodeTime 用 time.RFC3339Nano，长度随小数位变化：整秒没有小数，带小数时
-- '.'(0x2E) 的字典序小于 'Z'(0x5A)。SQLite 按 TEXT 字典序比较与排序，于是
-- ts >= '2026-...00Z' 会漏掉同一秒内所有带小数的行，ORDER BY ts 也会在整秒内错序。
-- 固定宽度后字符串序就是时间序，since/until、purge、ClaimJob 和会话过期的比较才可靠。
--
-- strftime 能解析旧的 RFC3339（含纳秒），%f 输出毫秒精度，亚毫秒部分被截断（与
-- 新的 encodeTime 一致）。空串与 NULL 原样保留，避免把“没有值”写成 NULL/空串互换，
-- 也避免 NOT NULL 列因为 NULL 而让迁移失败。
UPDATE tenants SET
    created_at = CASE WHEN created_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at) ELSE created_at END;

UPDATE tokens SET
    created_at   = CASE WHEN created_at   <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at)   ELSE created_at END,
    last_used_at = CASE WHEN last_used_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', last_used_at) ELSE last_used_at END,
    revoked_at   = CASE WHEN revoked_at   <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', revoked_at)   ELSE revoked_at END;

UPDATE audit_records SET
    ts = CASE WHEN ts <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', ts) ELSE ts END;

UPDATE jobs SET
    created_at      = CASE WHEN created_at      <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at)      ELSE created_at END,
    updated_at      = CASE WHEN updated_at      <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', updated_at)      ELSE updated_at END,
    next_attempt_at = CASE WHEN next_attempt_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', next_attempt_at) ELSE next_attempt_at END;

UPDATE accounts SET
    created_at    = CASE WHEN created_at    <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at)    ELSE created_at END,
    last_login_at = CASE WHEN last_login_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', last_login_at) ELSE last_login_at END;

UPDATE project_members SET
    created_at = CASE WHEN created_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at) ELSE created_at END;

UPDATE sessions SET
    created_at   = CASE WHEN created_at   <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at)   ELSE created_at END,
    expires_at   = CASE WHEN expires_at   <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', expires_at)   ELSE expires_at END,
    last_seen_at = CASE WHEN last_seen_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', last_seen_at) ELSE last_seen_at END,
    revoked_at   = CASE WHEN revoked_at   <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', revoked_at)   ELSE revoked_at END;

UPDATE idempotency_keys SET
    created_at = CASE WHEN created_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at) ELSE created_at END,
    updated_at = CASE WHEN updated_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) ELSE updated_at END;

UPDATE project_invites SET
    created_at = CASE WHEN created_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', created_at) ELSE created_at END,
    expires_at = CASE WHEN expires_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', expires_at) ELSE expires_at END,
    used_at    = CASE WHEN used_at    <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', used_at)    ELSE used_at END,
    revoked_at = CASE WHEN revoked_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', revoked_at) ELSE revoked_at END;

-- 本迁移的 schema_migrations 行在迁移体之后才写入，因此这里只规范化历史行。
UPDATE schema_migrations SET
    applied_at = CASE WHEN applied_at <> '' THEN strftime('%Y-%m-%dT%H:%M:%fZ', applied_at) ELSE applied_at END;
