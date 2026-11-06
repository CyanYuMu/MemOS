CREATE TABLE memory_event_sequence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);
INSERT INTO memory_event_sequence(id, value) VALUES(1, 0);
CREATE TABLE memory_events (
    sequence INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(tenant_id, event_key)
);
CREATE INDEX memory_events_tenant_sequence ON memory_events(tenant_id, sequence);
