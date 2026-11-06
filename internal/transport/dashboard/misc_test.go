package dashboard

import (
	"os"
	"path/filepath"
	"testing"
)

// 前端版本文件是排查用的附加信息：正常能读出来，缺失或损坏都不能影响启动。
func TestReadFrontendMeta(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "frontend.json")
	body := `{"path":"../memory-console","branch":"main","commit":"0d4c45c8","dirty":false}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	meta := readFrontendMeta(dir)
	if meta["commit"] != "0d4c45c8" || meta["branch"] != "main" || meta["path"] != "../memory-console" {
		t.Fatalf("解析结果不符: %+v", meta)
	}
}

func TestReadFrontendMetaMissingOrBroken(t *testing.T) {
	cases := []struct {
		name  string
		setup func(t *testing.T, dir string)
	}{
		{"目录为空", func(*testing.T, string) {}},
		{"webDir 为空", func(*testing.T, string) {}},
		{"内容不是 JSON", func(t *testing.T, dir string) {
			t.Helper()
			if err := os.WriteFile(filepath.Join(dir, "frontend.json"), []byte("not json"), 0o600); err != nil {
				t.Fatalf("write: %v", err)
			}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			tc.setup(t, dir)
			if tc.name == "webDir 为空" {
				dir = ""
			}
			if meta := readFrontendMeta(dir); meta != nil {
				t.Fatalf("应返回 nil，实际 %+v", meta)
			}
		})
	}
}
