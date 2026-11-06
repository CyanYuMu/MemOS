package migrations_test

import (
	"strconv"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/migrations"
)

// schema_migrations 只按整数版本记录，不记文件名。两个分支各自从旧的 main 取号时
// 会得到同一版本的两份文件；单看任一分支都没问题，合并后才暴露，后应用的第二个文件
// 会因校验和不一致让进程拒绝启动。这里直接对嵌入目录做唯一性检查，合并提交在 CI
// 就会失败，而不是等到某个库启动时才发现。
func TestMigrationVersionsAreUnique(t *testing.T) {
	t.Parallel()

	entries, err := migrations.FS.ReadDir(".")
	if err != nil {
		t.Fatalf("read migrations: %v", err)
	}
	seen := make(map[int]string, len(entries))
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".sql") {
			continue
		}
		version, err := strconv.Atoi(strings.SplitN(name, "_", 2)[0])
		if err != nil {
			t.Fatalf("迁移 %q 的文件名不是 <数字>_<名称>.sql: %v", name, err)
		}
		if prev, ok := seen[version]; ok {
			t.Fatalf("迁移版本 %04d 被 %s 与 %s 同时占用；合并前请 rebase 到最新 main 并重排编号", version, prev, name)
		}
		seen[version] = name
	}
	if len(seen) == 0 {
		t.Fatal("没有找到任何迁移文件")
	}
}
