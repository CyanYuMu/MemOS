// Package jobs 是异步写入任务的运维用例：死信查询与人工重放。
// 重放是否安全的判定在这里，transport 只做编解码与授权。
package jobs
