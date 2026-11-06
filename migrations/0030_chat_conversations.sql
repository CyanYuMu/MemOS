CREATE TABLE chat_conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX chat_conversations_owner ON chat_conversations(tenant_id, account_id, created_at DESC, id DESC);
