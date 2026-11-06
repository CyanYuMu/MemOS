package domain

import "time"

// KnowledgeBase 是上游按租户隔离的知识库。控制面不落库，只做协议转换与授权。
type KnowledgeBase struct {
	ID          string
	Name        string
	Description string
	FileCount   int
	TotalSize   int64
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// KnowledgeBasePage 是控制台分页后的知识库列表。
type KnowledgeBasePage struct {
	Items []KnowledgeBase
	Total int
}

// KnowledgeFile 是知识库里的一个文件条目（上游 `public_file_view` 的投影）。
type KnowledgeFile struct {
	ID              string
	KnowledgeBaseID string
	Name            string
	Type            string
	Status          string
	Size            int64
	SizeMB          float64
	CreatedAt       time.Time
}

// KnowledgeFilePage 是文件列表的一页。
type KnowledgeFilePage struct {
	Items []KnowledgeFile
	Total int
}

// KnowledgeFileInput 是待加入知识库的文件。Content 可以是 URL、data URL 或裸 base64，
// 与上游 cloud 契约一致；控制面不解释内容，只透传。
type KnowledgeFileInput struct {
	Name    string
	Content string
	Type    string
}

// KnowledgeBaseUpdate 是知识库改名的入参；nil 字段表示不修改。
type KnowledgeBaseUpdate struct {
	TenantID    string
	ID          string
	Name        *string
	Description *string
}

// KnowledgeFilesQuery 是知识库文件分页查询的入参。
type KnowledgeFilesQuery struct {
	TenantID        string
	KnowledgeBaseID string
	Page            int
	Size            int
}

// KnowledgeFilesAdd 是向知识库添加文件的入参；FileType 为空时由上游判断。
type KnowledgeFilesAdd struct {
	TenantID        string
	KnowledgeBaseID string
	Files           []KnowledgeFileInput
	FileType        string
}
