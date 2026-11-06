// Package upstream 是 memos-api（/product/*）的 HTTP 客户端。
// 负责超时与重试、响应体积保护（流式剥离 metadata.sources 等大字段）、时间口径归一（普通写入与 Dream 产物不同），
// 并把上游的已知怪癖（批量取返回 422、delete 带 user_id 静默失败等）挡在这一层之内。
package upstream
