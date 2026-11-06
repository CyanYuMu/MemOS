CREATE TABLE onboarding_accounts (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending','active','dismissed','completed')),
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
INSERT INTO onboarding_accounts(account_id,status,updated_at)
SELECT id,'dismissed',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM accounts;
CREATE TABLE onboarding_projects (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    step INTEGER NOT NULL CHECK (step BETWEEN 0 AND 6),
    chat_checkpoint TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(account_id,project_id)
);
