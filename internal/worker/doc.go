// Package worker 承载进程内的后台任务：异步写入（add_message 的后台抽取）、过期清理、崩溃遗留任务回收。
// 任务状态落库，进程重启后可恢复；停机时先排空队列再退出。
package worker
