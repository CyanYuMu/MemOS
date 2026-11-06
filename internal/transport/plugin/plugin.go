// Package plugin 发布 hermes 记忆插件的发布包，让没有源码的机器用一行命令安装。
package plugin

import (
	"bytes"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Server 提供 /graphmemory/{<agent>_plugin_install.sh,latest.txt,graphmemory-<version>.tar.gz}。
// 这些文件在镜像构建时放进 <root>/graphmemory/，是公开可读的，不做鉴权，
// 也不写审计：它们不接触任何租户数据。
type Server struct {
	// root 是发布包目录（镜像里是 /app/plugins）；为空时所有请求 404。
	root    string
	options Options
}

// New 构造发布入口。
type Options struct {
	ServiceURL string
	ConsoleURL string
}

func New(root string, options ...Options) *Server {
	s := &Server{root: strings.TrimSpace(root)}
	if len(options) > 0 {
		s.options = options[0]
	}
	return s
}

// 安装脚本按客户端命名（hermes_plugin_install.sh），以后接别的 coding agent 只加文件不改代码。
var scriptName = regexp.MustCompile(`^[a-z0-9_]+_plugin_install\.sh$`)

// packageName 只放行形如 graphmemory-0.2.12.tar.gz 的文件名，
// 其余路径（目录、点文件、子目录）一律 404。
var packageName = regexp.MustCompile(`^graphmemory-[0-9A-Za-z][0-9A-Za-z.+-]*\.tar\.gz$`)

// Handler 返回挂到 /memory/plugin/ 的处理器。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/graphmemory/", s.serveGraphMemory)
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		httpx.NotFoundJSON(w)
	})
	return httpx.MethodNotAllowedJSON(http.StripPrefix("/memory/plugin", mux))
}

func (s *Server) serveGraphMemory(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	name := strings.TrimPrefix(r.URL.Path, "/graphmemory/")
	switch {
	case name == "latest.txt":
		s.writeFile(w, r, name, "text/plain; charset=utf-8", "no-cache")
	case scriptName.MatchString(name):
		s.writeFile(w, r, name, "text/x-shellscript; charset=utf-8", "no-cache")
	case packageName.MatchString(name):
		s.writeFile(w, r, name, "application/gzip", "public, max-age=31536000, immutable")
	default:
		httpx.NotFoundJSON(w)
	}
}

func (s *Server) writeFile(w http.ResponseWriter, r *http.Request, name, contentType, cacheControl string) {
	if s.root == "" {
		httpx.NotFoundJSON(w)
		return
	}
	path := filepath.Join(s.root, "graphmemory", name)
	//nolint:gosec // name 已被上面的白名单限死在这三个文件名里，不含分隔符。
	f, err := os.Open(path)
	if err != nil {
		httpx.NotFoundJSON(w)
		return
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || info.IsDir() {
		httpx.NotFoundJSON(w)
		return
	}
	if scriptName.MatchString(name) {
		s.writeInstaller(w, r, f, name, contentType)
		return
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", cacheControl)
	// ServeContent 处理 HEAD、Range 与 If-None-Match；文件按版本命名后不再变化。
	http.ServeContent(w, r, name, info.ModTime(), f)
}

func shellQuote(value string) string { return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'" }

func (s *Server) writeInstaller(w http.ResponseWriter, r *http.Request, f *os.File, name, contentType string) {
	if s.options.ServiceURL == "" || s.options.ConsoleURL == "" {
		http.Error(w, "Plugin installation is not configured", http.StatusServiceUnavailable)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(f, (1<<20)+1))
	if err != nil || len(raw) > 1<<20 {
		http.Error(w, "Installer unavailable", http.StatusInternalServerError)
		return
	}
	var prefix strings.Builder
	prefix.WriteString("#!/usr/bin/env bash\n")
	for _, pair := range [][2]string{{"GRAPH_MEMORY_PLUGIN_BASE", s.options.ServiceURL + "/memory/plugin/graphmemory"}, {"GRAPH_MEMORY_GATEWAY_URL", s.options.ServiceURL}, {"GRAPH_MEMORY_KEYS_URL", s.options.ConsoleURL + "/cn/apikeys"}} {
		prefix.WriteString("if [ -z \"${" + pair[0] + ":-}\" ]; then " + pair[0] + "=" + shellQuote(pair[1]) + "; fi\nexport " + pair[0] + "\n")
	}
	body := append([]byte(prefix.String()), raw...)
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", "no-store")
	http.ServeContent(w, r, name, time.Time{}, bytes.NewReader(body))
}
