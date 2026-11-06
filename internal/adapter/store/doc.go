// Package store 是控制面自有数据的仓储实现：租户、API Token、控制台账号与会话、审计记录、异步任务、prompt 版本。
// 当前方案是 SQLite（纯 Go 驱动），集群里没有可共享的 PostgreSQL，取舍见 docs/ARCHITECTURE.md 的“存储”与“待决事项”。
// 仓储接口由 service 声明，换存储只应影响本包。
package store
