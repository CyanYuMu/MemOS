package mcp

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
)

const (
	initializeBody = `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}`
	toolsListBody  = `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`
)

func postMCP(t *testing.T, h http.Handler, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/mcp", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// 未认证的 initialize 必须 401：客户端不能在凭据无效时先建立连接、看到工具清单。
func TestMCPHandlerRejectsUnauthenticatedInitialize(t *testing.T) {
	srv := newTestServer(t, nil, nil)

	rec := postMCP(t, srv.Handler(), "", initializeBody)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("initialize 无 token = %d, want 401; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "invalid_token") {
		t.Fatalf("401 body 缺少 invalid_token: %s", rec.Body.String())
	}
	if got := rec.Header().Get("WWW-Authenticate"); !strings.Contains(got, "invalid_token") {
		t.Fatalf("WWW-Authenticate = %q", got)
	}
}

func TestMCPHandlerRejectsUnauthenticatedToolsList(t *testing.T) {
	srv := newTestServer(t, nil, nil)

	rec := postMCP(t, srv.Handler(), "", toolsListBody)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("tools/list 无 token = %d, want 401; body=%s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), `"tools"`) {
		t.Fatalf("未认证响应泄漏了工具清单: %s", rec.Body.String())
	}
}

func TestMCPHandlerArchivesProtocolFrames(t *testing.T) {
	tok := "memos_test"
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	recorder, err := audit.New(&captureAuditStore{}, slog.Default(), audit.Options{File: path})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	defer func() { _ = recorder.Close() }()
	srv := newTestServerWith(t, nil, &tok, nil, recorder, memory.Options{})

	if rec := postMCP(t, srv.Handler(), tok, initializeBody); rec.Code != http.StatusOK {
		t.Fatalf("initialize = %d; body=%s", rec.Code, rec.Body.String())
	}
	if rec := postMCP(t, srv.Handler(), tok, toolsListBody); rec.Code != http.StatusOK {
		t.Fatalf("tools/list = %d; body=%s", rec.Code, rec.Body.String())
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read archive: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	var frames []string
	raw := 0
	for _, line := range lines {
		if strings.Contains(line, `"tool":"mcp.request"`) {
			raw++
			if !strings.Contains(line, `"body":"{\"jsonrpc\":\"2.0\"`) ||
				!strings.Contains(line, `"tenant":"cube-a"`) {
				t.Fatalf("raw request line = %s", line)
			}
			continue
		}
		frames = append(frames, line)
	}
	if raw != 2 || len(frames) != 2 {
		t.Fatalf("archive raw=%d frames=%d, want 2/2: %s", raw, len(frames), data)
	}
	if !strings.Contains(frames[0], `"tool":"mcp.initialize"`) ||
		!strings.Contains(frames[0], `"clientInfo"`) {
		t.Fatalf("initialize line = %s", frames[0])
	}
	if !strings.Contains(frames[1], `"tool":"mcp.tools/list"`) {
		t.Fatalf("tools/list line = %s", frames[1])
	}
}

// 未认证的原始请求也要落归档，否则审计看不到谁在拿错误 token 探测。
func TestMCPHandlerArchivesUnauthenticatedRawRequest(t *testing.T) {
	tok := "memos_test"
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	recorder, err := audit.New(&captureAuditStore{}, slog.Default(), audit.Options{File: path})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	defer func() { _ = recorder.Close() }()
	srv := newTestServerWith(t, nil, &tok, nil, recorder, memory.Options{})

	if rec := postMCP(t, srv.Handler(), "", initializeBody); rec.Code != http.StatusUnauthorized {
		t.Fatalf("initialize 无 token = %d, want 401", rec.Code)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read archive: %v", err)
	}
	if !strings.Contains(string(data), `"tool":"mcp.request"`) ||
		!strings.Contains(string(data), `"ok":false`) ||
		!strings.Contains(string(data), `"http_status":401`) ||
		!strings.Contains(string(data), `\"method\":\"initialize\"`) {
		t.Fatalf("unauthorized raw archive = %s", data)
	}
}

// 与线上 stateless_http=True 对齐：initialize 不下发会话，后续请求也不需要回传。
func TestMCPHandlerIsStateless(t *testing.T) {
	tok := "memos_test"
	srv := newTestServer(t, nil, &tok)

	initRec := postMCP(t, srv.Handler(), tok, initializeBody)
	if initRec.Code != http.StatusOK {
		t.Fatalf("initialize 带 token = %d; body=%s", initRec.Code, initRec.Body.String())
	}
	if sid := initRec.Header().Get("Mcp-Session-Id"); sid != "" {
		t.Fatalf("stateless 不应下发 Mcp-Session-Id，得到 %q", sid)
	}

	listRec := postMCP(t, srv.Handler(), tok, toolsListBody)
	if listRec.Code != http.StatusOK {
		t.Fatalf("tools/list 不带会话 = %d; body=%s", listRec.Code, listRec.Body.String())
	}
	var resp struct {
		Result struct {
			Tools []struct {
				Name string `json:"name"`
			} `json:"tools"`
		} `json:"result"`
	}
	if err := json.Unmarshal(listRec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析 tools/list: %v; body=%s", err, listRec.Body.String())
	}
	if len(resp.Result.Tools) != 6 {
		t.Fatalf("tools/list 返回 %d 个工具，期望 6", len(resp.Result.Tools))
	}
}
