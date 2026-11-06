package rest

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/getkin/kin-openapi/openapi3"
	"github.com/getkin/kin-openapi/openapi3filter"
	"github.com/getkin/kin-openapi/routers/gorillamux"
)

// TestOpenAPIContract 用规范校验真实请求与响应，防止 spec 与路由漂移。
func TestOpenAPIContract(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	loader := openapi3.NewLoader()
	doc, err := loader.LoadFromFile(filepath.Join("..", "..", "..", "api", "openapi.yaml"))
	if err != nil {
		t.Fatalf("load openapi: %v", err)
	}
	if err := doc.Validate(ctx); err != nil {
		t.Fatalf("validate openapi: %v", err)
	}
	if got := len(doc.Paths.Map()); got != 6 {
		t.Fatalf("openapi paths = %d, want 6", got)
	}
	router, err := gorillamux.NewRouter(doc)
	if err != nil {
		t.Fatalf("new router: %v", err)
	}

	cases := []struct {
		path string
		body string
	}{
		{"/memory/add", `{"conversation_first_message":"hi","messages":[{"role":"user","content":"hi"}],"tags":["t"],"idempotency_key":"k1"}`},
		{"/memory/search", `{"query":"hi","top_k":20}`},
		{"/memory/update", `{"memory_id":"mem-1","content":"coffee"}`},
		{"/memory/delete", `{"memory_ids":["mem-1"]}`},
		{"/memory/feedback", `{"feedback_content":"prefer tea"}`},
		{"/memory/profile", `{"include_preference":true,"current":1,"size":5}`},
	}
	srv := newTestServer(t, &fakeUpstream{}, nil)
	for _, tc := range cases {
		t.Run(tc.path, func(t *testing.T) {
			req := newJSONRequest(ctx, t, tc.path, tc.body)
			route, pathParams, err := router.FindRoute(req)
			if err != nil {
				t.Fatalf("find route: %v", err)
			}
			input := &openapi3filter.RequestValidationInput{
				Request: req, PathParams: pathParams, Route: route,
				Options: &openapi3filter.Options{AuthenticationFunc: openapi3filter.NoopAuthenticationFunc},
			}
			if err := openapi3filter.ValidateRequest(ctx, input); err != nil {
				t.Fatalf("validate request: %v", err)
			}

			// 请求校验会读掉 body，实际调用要用新的请求。
			rec := httptest.NewRecorder()
			srv.Handler().ServeHTTP(rec, newJSONRequest(ctx, t, tc.path, tc.body))
			if rec.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
			}
			resp := &openapi3filter.ResponseValidationInput{
				RequestValidationInput: input,
				Status:                 rec.Code,
				Header:                 rec.Header(),
				Body:                   io.NopCloser(bytes.NewReader(rec.Body.Bytes())),
			}
			if err := openapi3filter.ValidateResponse(ctx, resp); err != nil {
				t.Fatalf("validate response: %v\n%s", err, rec.Body.String())
			}
		})
	}
}

func newJSONRequest(ctx context.Context, t *testing.T, path, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequestWithContext(ctx, http.MethodPost, path, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	return req
}
