package httpx

import (
	"net/http"
)

// MethodNotAllowedJSON 把 ServeMux 内建的方法不匹配响应换成统一 JSON 信封。
//
// Go 1.22 的 ServeMux 在路径命中但方法不匹配时自动回 405 和 Allow 头，正文却是
// net/http 的纯文本 "Method Not Allowed"。控制台所有入口都承诺 JSON 信封，
// 前端也按 code 分支，因此这里只替换 405 的正文，其余响应原样透传。
// 注意 ServeMux 已经写好状态码与 Allow 头，包装器不再重复写头。
func MethodNotAllowedJSON(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(&methodNotAllowedWriter{ResponseWriter: w}, r)
	})
}

type methodNotAllowedWriter struct {
	http.ResponseWriter
	suppress bool
}

func (w *methodNotAllowedWriter) WriteHeader(status int) {
	if status == http.StatusMethodNotAllowed {
		w.suppress = true
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		// ServeMux 不写 Content-Length；由 net/http 按实际写入补齐。
		w.ResponseWriter.WriteHeader(status)
		_, _ = w.ResponseWriter.Write([]byte(`{"code":40500,"data":null,"message":"方法不允许"}` + "\n"))
		return
	}
	w.ResponseWriter.WriteHeader(status)
}

func (w *methodNotAllowedWriter) Write(p []byte) (int, error) {
	if w.suppress {
		// 已经用信封写过正文，丢弃 ServeMux 的纯文本。
		return len(p), nil
	}
	return w.ResponseWriter.Write(p)
}

// Unwrap 让 http.ResponseController 能拿到底层 ResponseWriter 的 Flush：
// SSE 路由经这层包装后仍必须能逐帧 flush。
func (w *methodNotAllowedWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
