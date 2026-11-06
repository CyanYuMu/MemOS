-- API Token 可选过期时间。空串表示永不过期（静态 Token 与历史行）。
ALTER TABLE tokens ADD COLUMN expires_at TEXT NOT NULL DEFAULT '';
