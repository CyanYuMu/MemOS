-- 零信任网关会传 email；账号表存一份，便于展示与后续通知。
-- 本地账密账号可以为空。
ALTER TABLE accounts ADD COLUMN email TEXT NOT NULL DEFAULT '';
