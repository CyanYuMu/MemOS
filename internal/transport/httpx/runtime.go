package httpx

import (
	"encoding/json"
	"net/http"
)

// PublicRuntimeConfig 使同一前端产物随部署取地址；未配置时显式返回空值。
func PublicRuntimeConfig(serviceURL, consoleURL string) http.Handler {
	config, _ := json.Marshal(map[string]string{"serviceBase": serviceURL, "consoleBase": consoleURL})
	body := append([]byte("window.__GRAPH_MEMORY_CONFIG__="), config...)
	body = append(body, ';', '\n')
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.Method == http.MethodGet {
			_, _ = w.Write(body)
		}
	})
}
