// Package version 持有构建期注入的版本信息。
package version

import "fmt"

// 构建时由 -ldflags "-X github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/version.Version=..." 注入。
var (
	Version = "dev"
	Commit  = "none"
	Date    = "unknown"
)

// String 返回一行可读的版本描述。
func String() string {
	return fmt.Sprintf("%s (commit %s, built %s)", Version, Commit, Date)
}
