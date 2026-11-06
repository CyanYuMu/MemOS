// Package migrations 内嵌数据库迁移脚本。store 在打开数据库时按文件名顺序执行。
package migrations

import "embed"

// FS 是 migrations/*.sql 的只读文件系统。
//
//go:embed *.sql
var FS embed.FS
