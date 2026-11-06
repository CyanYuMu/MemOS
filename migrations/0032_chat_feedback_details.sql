-- 兼容旧评分，文字意见独立于Pi历史保存。
ALTER TABLE chat_feedback ADD COLUMN comment TEXT NOT NULL DEFAULT '';
ALTER TABLE chat_feedback ADD COLUMN conversation_id TEXT NOT NULL DEFAULT '';
ALTER TABLE chat_feedback ADD COLUMN answer_excerpt TEXT NOT NULL DEFAULT '';
