package upstream

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func contractServer(t *testing.T, body string, status int) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/contract" {
			t.Errorf("path = %s", r.URL.Path)
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func TestCheckContractAcceptsMatchingVersion(t *testing.T) {
	t.Parallel()

	client := newTestClient(t, contractServer(t, `{
		"contract_version": 2,
		"time": {"basis": "utc"},
		"sources": {"default": "omitted"},
		"top_k_max": 25
	}`, http.StatusOK), 1<<20)

	if err := client.CheckContract(context.Background()); err != nil {
		t.Fatalf("CheckContract: %v", err)
	}
}

func TestCheckContractRejectsMismatches(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		body string
		code int
		want string
	}{
		{
			name: "older upstream",
			body: `{"contract_version":1,"time":{"basis":"utc"},"sources":{"default":"omitted"},"top_k_max":25}`,
			code: http.StatusOK,
			want: "contract version 1",
		},
		{
			name: "time basis changed",
			body: `{"contract_version":2,"time":{"basis":"legacy_local"},"sources":{"default":"omitted"},"top_k_max":25}`,
			code: http.StatusOK,
			want: "time basis",
		},
		{
			name: "sources default changed",
			body: `{"contract_version":2,"time":{"basis":"utc"},"sources":{"default":"included"},"top_k_max":25}`,
			code: http.StatusOK,
			want: "sources default",
		},
		{
			name: "top_k cap changed",
			body: `{"contract_version":2,"time":{"basis":"utc"},"sources":{"default":"omitted"},"top_k_max":10}`,
			code: http.StatusOK,
			want: "top_k_max",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			client := newTestClient(t, contractServer(t, tt.body, tt.code), 1<<20)
			err := client.CheckContract(context.Background())
			if err == nil {
				t.Fatal("expected contract mismatch")
			}
			if !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error = %q, want substring %q", err, tt.want)
			}
		})
	}
}

// 上游没有契约端点时必须判定为不健康：旧镜像已经下线，不存在需要放行的情况，
// 静默跳过校验只会让版本/时间口径不一致的问题延后发现。
func TestCheckContractRejectsUpstreamWithoutEndpoint(t *testing.T) {
	t.Parallel()

	client := newTestClient(t, contractServer(t, `{"detail":"Not Found"}`, http.StatusNotFound), 1<<20)
	err := client.CheckContract(context.Background())
	if err == nil {
		t.Fatal("缺少契约端点时应报错")
	}
	if !strings.Contains(err.Error(), "端点不存在") {
		t.Fatalf("error = %q, 应说明是端点不存在", err)
	}
}

func TestCheckContractGivesUpOnAHungUpstream(t *testing.T) {
	t.Parallel()

	client := newTestClient(t, http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		// Hold the response until the client gives up, so the handler unwinds with the
		// request instead of outliving the test server.
		<-r.Context().Done()
	}), 1<<20)

	started := time.Now()
	if err := client.CheckContract(context.Background()); err == nil {
		t.Fatal("expected timeout error")
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Fatalf("contract check waited %s, must stay under the readiness budget", elapsed)
	}
}
