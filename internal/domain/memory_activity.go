package domain

// MemoryWriteSnapshot 固定写入后首次读回的实体，不用当前值重建历史。
type MemoryWriteSnapshot struct {
	SchemaVersion int      `json:"schema_version"`
	TenantID      string   `json:"tenant_id"`
	UserID        string   `json:"user_id"`
	SessionID     string   `json:"session_id"`
	CapturedAt    UTCTime  `json:"captured_at"`
	Memories      []Memory `json:"memories"`
	Truncated     bool     `json:"truncated"`
}

// MemoryWriteOutput 只用于内部完成审计；新增可选快照兼容历史ID-only输出。
type MemoryWriteOutput struct {
	MemoryIDs []string             `json:"added_memory_ids"`
	Snapshot  *MemoryWriteSnapshot `json:"snapshot,omitempty"`
}
