package httpx

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func newSPADir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "index.html"), []byte("<html>app</html>"), 0o600); err != nil {
		t.Fatalf("write index: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "assets"), 0o750); err != nil {
		t.Fatalf("mkdir assets: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "assets", "app-abc123.js"), []byte("console.log(1)"), 0o600); err != nil {
		t.Fatalf("write asset: %v", err)
	}
	return dir
}

func TestSPA(t *testing.T) {
	t.Parallel()
	dir := newSPADir(t)
	h, err := SPA(dir)
	if err != nil {
		t.Fatalf("SPA: %v", err)
	}

	tests := []struct {
		name      string
		method    string
		path      string
		want      int
		wantBody  string
		noStore   bool
		cacheable bool
	}{
		{name: "root", method: http.MethodGet, path: "/", want: http.StatusOK, wantBody: "<html>app</html>", noStore: true},
		{name: "asset", method: http.MethodGet, path: "/assets/app-abc123.js", want: http.StatusOK, wantBody: "console.log(1)", cacheable: true},
		{name: "spa route fallback", method: http.MethodGet, path: "/projects/cube-a", want: http.StatusOK, wantBody: "<html>app</html>", noStore: true},
		{name: "missing file", method: http.MethodGet, path: "/assets/missing.js", want: http.StatusNotFound},
		{name: "traversal", method: http.MethodGet, path: "/../etc/passwd", want: http.StatusNotFound},
		{name: "post", method: http.MethodPost, path: "/", want: http.StatusNotFound},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			req := httptest.NewRequestWithContext(t.Context(), tc.method, tc.path, nil)
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d body=%s", rec.Code, tc.want, rec.Body.String())
			}
			if tc.wantBody != "" && rec.Body.String() != tc.wantBody {
				t.Fatalf("body = %q", rec.Body.String())
			}
			cache := rec.Header().Get("Cache-Control")
			if tc.noStore && cache != "no-store" {
				t.Fatalf("Cache-Control = %q, want no-store", cache)
			}
			if tc.cacheable && cache == "" {
				t.Fatal("静态资源应带 Cache-Control")
			}
		})
	}
}

func TestSPARejectsBadDir(t *testing.T) {
	t.Parallel()
	if _, err := SPA(""); err == nil {
		t.Fatal("空目录应被拒绝")
	}
	if _, err := SPA(t.TempDir()); err == nil {
		t.Fatal("缺少 index.html 应被拒绝")
	}
}

// TestSPARejectsSymlinkEscape 静态目录里指向目录外的符号链接不能被读到。
func TestSPARejectsSymlinkEscape(t *testing.T) {
	t.Parallel()
	dir := newSPADir(t)
	outside := filepath.Join(t.TempDir(), "secret.txt")
	if err := os.WriteFile(outside, []byte("secret"), 0o600); err != nil {
		t.Fatalf("write outside: %v", err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "assets", "leak.txt")); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	h, err := SPA(dir)
	if err != nil {
		t.Fatalf("SPA: %v", err)
	}
	req := httptest.NewRequestWithContext(t.Context(), http.MethodGet, "/assets/leak.txt", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 body=%s", rec.Code, rec.Body.String())
	}
}
