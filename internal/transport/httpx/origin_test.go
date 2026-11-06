package httpx

import (
	"net/http/httptest"
	"testing"
)

func TestSameOriginPost(t *testing.T) {
	cases := []struct {
		name    string
		headers map[string]string
		want    bool
	}{
		{name: "没有浏览器头（非浏览器客户端）", want: true},
		{name: "同源表单提交 Origin 为 null（Chrome 实际行为）", headers: map[string]string{
			"Sec-Fetch-Site": "same-origin",
			"Origin":         "null",
		}, want: true},
		{name: "Origin null 但缺少 Sec-Fetch 元数据（沙箱 iframe 可伪造）", headers: map[string]string{
			"Origin": "null",
		}, want: false},
		{name: "Origin null 且跨站", headers: map[string]string{
			"Sec-Fetch-Site": "cross-site",
			"Origin":         "null",
		}, want: false},
		{name: "Origin null 且同站不同源", headers: map[string]string{
			"Sec-Fetch-Site": "same-site",
			"Origin":         "null",
		}, want: false},
		{name: "顶层导航 none", headers: map[string]string{"Sec-Fetch-Site": "none"}, want: true},
		{name: "同源且来源一致", headers: map[string]string{
			"Sec-Fetch-Site": "same-origin",
			"Origin":         "http://127.0.0.1:18090",
		}, want: true},
		{name: "同源但来源不同", headers: map[string]string{
			"Sec-Fetch-Site": "same-origin",
			"Origin":         "https://evil.example",
		}, want: false},
		{name: "跨站提交", headers: map[string]string{
			"Sec-Fetch-Site": "cross-site",
			"Origin":         "https://evil.example",
		}, want: false},
		{name: "同站不同端口也拒绝", headers: map[string]string{
			"Sec-Fetch-Site": "same-site",
			"Origin":         "http://127.0.0.1:18090",
		}, want: false},
		{name: "网关改写 Host 时看 X-Forwarded-Host", headers: map[string]string{
			"Sec-Fetch-Site":   "same-origin",
			"Origin":           "https://console.example.com",
			"X-Forwarded-Host": "console.example.com",
		}, want: true},
		{name: "多级代理转发头取第一段", headers: map[string]string{
			"Sec-Fetch-Site":   "same-origin",
			"Origin":           "https://console.example.com",
			"X-Forwarded-Host": "console.example.com, inner.svc",
		}, want: true},
		{name: "来源无法解析", headers: map[string]string{
			"Sec-Fetch-Site": "same-origin",
			"Origin":         "::not-a-url::",
		}, want: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequestWithContext(t.Context(), "POST", "http://127.0.0.1:18090/__device", nil)
			for k, v := range tc.headers {
				r.Header.Set(k, v)
			}
			if got := SameOriginPost(r); got != tc.want {
				t.Fatalf("SameOriginPost() = %v, want %v", got, tc.want)
			}
		})
	}
}
