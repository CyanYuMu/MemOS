package plugin

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func newServer(t *testing.T) *Server {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, "graphmemory")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	files := map[string]string{
		"hermes_plugin_install.sh":  "#!/bin/sh\n",
		"latest.txt":                "version=0.2.12\n",
		"graphmemory-0.2.12.tar.gz": "pkg",
		"secret.txt":                "nope",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	return New(root, Options{ServiceURL: "https://gateway.example", ConsoleURL: "https://console.example"})
}

func TestHandler(t *testing.T) {
	s := newServer(t)
	h := s.Handler()

	cases := []struct {
		name        string
		method      string
		path        string
		status      int
		contentType string
	}{
		{"脚本", http.MethodGet, "/memory/plugin/graphmemory/hermes_plugin_install.sh", http.StatusOK, "text/x-shellscript; charset=utf-8"},
		{"版本清单", http.MethodGet, "/memory/plugin/graphmemory/latest.txt", http.StatusOK, "text/plain; charset=utf-8"},
		{"发布包", http.MethodHead, "/memory/plugin/graphmemory/graphmemory-0.2.12.tar.gz", http.StatusOK, "application/gzip"},
		{"白名单外的文件", http.MethodGet, "/memory/plugin/graphmemory/secret.txt", http.StatusNotFound, ""},
		{"目录列表", http.MethodGet, "/memory/plugin/graphmemory/", http.StatusNotFound, ""},
		{"文件名越界", http.MethodGet, "/memory/plugin/graphmemory/graphmemory-x/../../../secret.tar.gz", http.StatusTemporaryRedirect, ""},
		{"写方法", http.MethodPost, "/memory/plugin/graphmemory/hermes_plugin_install.sh", http.StatusMethodNotAllowed, ""},
		{"未挂载前缀", http.MethodGet, "/other/graphmemory/install.sh", http.StatusNotFound, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), tc.method, tc.path, nil))
			if rec.Code != tc.status {
				t.Fatalf("status = %d, want %d", rec.Code, tc.status)
			}
			if tc.contentType != "" && rec.Header().Get("Content-Type") != tc.contentType {
				t.Fatalf("content-type = %q, want %q", rec.Header().Get("Content-Type"), tc.contentType)
			}
		})
	}
}

func TestHandlerWithoutRoot(t *testing.T) {
	rec := httptest.NewRecorder()
	New("").Handler().ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/memory/plugin/graphmemory/hermes_plugin_install.sh", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}
