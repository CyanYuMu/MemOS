package httpx

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestLoggedOutGate(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	gate := LoggedOutGate(next)

	cases := []struct {
		name     string
		method   string
		path     string
		accept   string
		cookie   bool
		want     int
		contains string
	}{
		{name: "无标记放行", method: http.MethodGet, path: "/api/auth/me", want: http.StatusNoContent},
		{name: "有标记的接口请求 401", method: http.MethodGet, path: "/api/auth/me", cookie: true, want: http.StatusUnauthorized},
		{name: "有标记的导航给退出页", method: http.MethodGet, path: "/cn/projects", accept: "text/html", cookie: true, want: http.StatusOK, contains: "已退出登录"},
		{name: "有标记的 POST 401", method: http.MethodPost, path: "/__invite/x", cookie: true, want: http.StatusUnauthorized},
		{name: "重新登录放行", method: http.MethodGet, path: "/__relogin", accept: "text/html", cookie: true, want: http.StatusNoContent},
		{name: "重复登出放行", method: http.MethodGet, path: "/__logout", accept: "text/html", cookie: true, want: http.StatusNoContent},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequestWithContext(t.Context(), tc.method, tc.path, nil)
			if tc.accept != "" {
				req.Header.Set("Accept", tc.accept)
			}
			if tc.cookie {
				req.AddCookie(&http.Cookie{Name: LoggedOutCookie, Value: "1"})
			}
			rec := httptest.NewRecorder()
			gate.ServeHTTP(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d, body=%s", rec.Code, tc.want, rec.Body.String())
			}
			if tc.contains != "" && !strings.Contains(rec.Body.String(), tc.contains) {
				t.Fatalf("body 缺少 %q: %s", tc.contains, rec.Body.String())
			}
		})
	}
}
