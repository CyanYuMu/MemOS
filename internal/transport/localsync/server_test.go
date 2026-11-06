package localsync

import (
	"errors"
	"net/http"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// TestMapErrorSplitsBodyTooLargeFromQuota 413 有两种含义，插件据此区分“重发小一点”
// 与“配额已满，别再试”。两者混用会让用户对 2 GiB 配额无能为力，也会把一次性
// 的超大请求误报成配额问题。
func TestMapErrorSplitsBodyTooLargeFromQuota(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name       string
		err        error
		wantStatus int
		wantCode   int
	}{
		{
			name:       "body too large",
			err:        httpx.ErrBodyTooLarge,
			wantStatus: http.StatusRequestEntityTooLarge,
			wantCode:   httpx.CodeBadRequest,
		},
		{
			name:       "wrapped body too large",
			err:        errors.Join(domain.ErrInvalidInput, httpx.ErrBodyTooLarge),
			wantStatus: http.StatusRequestEntityTooLarge,
			wantCode:   httpx.CodeBadRequest,
		},
		{
			name:       "quota exceeded",
			err:        domain.Wrap(domain.ErrQuotaExceeded, "put_chunk", "项目配额已满", nil),
			wantStatus: http.StatusRequestEntityTooLarge,
			wantCode:   httpx.CodePayloadTooLarge,
		},
		{
			name:       "invalid input",
			err:        domain.Invalid("manifest", "行数不匹配"),
			wantStatus: http.StatusBadRequest,
			wantCode:   httpx.CodeBadRequest,
		},
		{
			name:       "unknown",
			err:        errors.New("boom"),
			wantStatus: http.StatusInternalServerError,
			wantCode:   httpx.CodeInternal,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			status, code, msg := mapError(tc.err)
			if status != tc.wantStatus || code != tc.wantCode {
				t.Fatalf("mapError(%v) = (%d, %d, %q), want (%d, %d)",
					tc.err, status, code, msg, tc.wantStatus, tc.wantCode)
			}
			if msg == "" {
				t.Fatal("mapError returned an empty message")
			}
		})
	}
}
