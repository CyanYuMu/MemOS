package domain

// GraphMemory 是记忆图谱里的一条节点。时间保持上游返回的字面值，
// 由展示层决定口径（普通写入是假 UTC，Dream 产物是真 UTC）。
type GraphMemory struct {
	ID        string
	Content   string
	Type      string
	UserID    string
	SessionID string
	CreatedAt string
	Version   int
	History   []MemoryVersion
	Sources   []MemorySource
	UpdatedAt string
	SourceIDs []string
	// Truncated 表示 Content 是上游按 label_max 截断后的标签，正文需按 id 另取。
	Truncated bool
}

// GraphEdge 是记忆之间的有向关系（PARENT / MERGED_TO）。
type GraphEdge struct {
	Source string
	Target string
	Kind   string
}

// GraphPage 是一次图谱查询的结果。上游同时返回节点与边，
// Truncated 由上游的 total_nodes 与本次节点数比较得出。
type GraphPage struct {
	Nodes     []GraphMemory
	Edges     []GraphEdge
	Total     int
	Truncated bool
}

// EventMemoriesQuery 是事件记忆分页查询的入参；Types 不能为空。
type EventMemoriesQuery struct {
	TenantID string
	Types    []string
	Query    string
	Page     int
	Size     int
}
