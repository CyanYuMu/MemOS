package domain

// MemoryEvent 是已确认操作的不可变投影；权限由租户作用域决定，call_id仅用于关联。
type MemoryEvent struct {
	Sequence   int64
	ID         string
	Key        string
	TenantID   string
	UserID     string
	Kind       string
	Status     string
	CallID     string
	SessionID  string
	OccurredAt UTCTime
	IDs        []string
	Total      int
	Memories   []Memory
	Before     []Memory
	Truncated  bool
}

type MemoryEventPage struct {
	Records []MemoryEvent
	Cursor  string
	HasMore bool
}
