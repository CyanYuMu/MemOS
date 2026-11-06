-- 回答评分按账号与项目隔离，不改变记忆内容。
CREATE TABLE chat_feedback (
 tenant_id TEXT NOT NULL,
 account_id TEXT NOT NULL,
 response_id TEXT NOT NULL,
 vote INTEGER NOT NULL CHECK (vote IN (-1, 1)),
 updated_at TEXT NOT NULL,
 PRIMARY KEY (tenant_id, account_id, response_id)
);
