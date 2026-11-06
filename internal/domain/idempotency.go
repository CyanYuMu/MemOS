package domain

import "time"

// 请求去重记录的状态。
// in_progress：已被请求占用；accepted：异步入队成功、等待 worker；
// succeeded：结果已落库可重放。
const (
	IdempotencyInProgress = "in_progress"
	IdempotencyAccepted   = "accepted"
	IdempotencySucceeded  = "succeeded"
)

// IdempotencyRecord 是一次写入的请求去重记录。Key 由 service 按归一内容派生，
// 调用方不需要感知；RequestHash 用来拒绝“同键不同请求”。
type IdempotencyRecord struct {
	TenantID    string
	Key         string
	Tool        string
	RequestHash string
	State       string
	Response    []byte
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

// IdempotencyOutcome 是把在途记录收敛到终态的入参；Response 为 nil 时保留原响应。
type IdempotencyOutcome struct {
	TenantID string
	Key      string
	Response []byte
	At       time.Time
}
