package httpx

import (
	"errors"
	"fmt"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// SPA 提供单页应用的静态产物：命中文件直接返回，未命中的页面请求回退到
// index.html（前端路由刷新不 404），带扩展名的未命中返回 404。
//
// 缓存策略：index.html 与未命中回退一律 no-store，避免发布后仍加载旧入口；
// 其余静态文件允许缓存 1 小时，文件名带内容哈希时由前端构建保证不会命中旧内容。
func SPA(dir string) (http.Handler, error) {
	dir = strings.TrimSpace(dir)
	if dir == "" {
		return nil, errors.New("httpx: 静态目录不能为空")
	}
	info, err := os.Stat(dir)
	if err != nil {
		return nil, fmt.Errorf("httpx: 静态目录不可用: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("httpx: %s 不是目录", dir)
	}
	if _, err := os.Stat(filepath.Join(dir, "index.html")); err != nil {
		return nil, fmt.Errorf("httpx: 静态目录缺少 index.html: %w", err)
	}
	// 目录本身若是符号链接，先解析成真实路径，后续所有包含判断都基于它，
	// 避免把“根在别处”的产物目录当成合法根。
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return nil, fmt.Errorf("httpx: 解析静态目录: %w", err)
	}
	dir = resolved
	files := http.FileServer(http.Dir(dir))

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		setSPAHeaders(w)
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			http.NotFound(w, r)
			return
		}
		// ServeFile 会拒绝含 ".." 的 URL，这里先显式拒绝，非 GET 页面请求得到 404 而不是 400。
		if strings.Contains(r.URL.Path, "..") {
			http.NotFound(w, r)
			return
		}
		name := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if name == "" {
			serveIndex(w, r, dir)
			return
		}
		if target, ok := safeFile(dir, name); ok {
			if info, err := os.Stat(target); err == nil && !info.IsDir() {
				w.Header().Set("Cache-Control", "public, max-age=3600")
				files.ServeHTTP(w, r)
				return
			}
		}
		if path.Ext(name) == "" {
			serveIndex(w, r, dir)
			return
		}
		http.NotFound(w, r)
	}), nil
}

// setSPAHeaders 给控制台页面加基础安全头。控制台是纯静态产物：
// 只加载同源脚本/样式，图标是 data: URI，没有外部资源与内联 <script>。
// 内联 style 属性在 React 组件里仍有使用，因此 style-src 保留 'unsafe-inline'。
func setSPAHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Referrer-Policy", "strict-origin-when-cross-origin")
	h.Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
	h.Set("Content-Security-Policy",
		"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "+
			"img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "+
			"frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
}

// safeFile 返回位于 dir 之内的目标路径；符号链接解析后仍在 dir 之外的一律拒绝。
func safeFile(dir, name string) (string, bool) {
	target := filepath.Join(dir, filepath.FromSlash(name))
	resolved, err := filepath.EvalSymlinks(target)
	if err != nil {
		return "", false
	}
	rel, err := filepath.Rel(dir, resolved)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return target, true
}

func serveIndex(w http.ResponseWriter, r *http.Request, dir string) {
	w.Header().Set("Cache-Control", "no-store")
	http.ServeFile(w, r, filepath.Join(dir, "index.html"))
}
