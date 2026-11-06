package metrics

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestMetricsScrapeContainsExpectedSeries(t *testing.T) {
	t.Parallel()

	m := New(Options{Version: "test-version", Role: "all"})
	m.ObserveHTTP(HTTPObservation{Role: "console", Method: "POST", Route: "/api/dashboard/project/list", Status: http.StatusOK, Duration: 12 * time.Millisecond})
	m.ObserveHTTP(HTTPObservation{Role: "console", Method: "GET", Route: "other", Status: http.StatusNotFound, Duration: time.Second})
	m.ObserveUpstream("/product/search", "ok", 2*time.Second)
	m.ObserveUpstream("/product/get_memory/{id}", "unavailable", 3*time.Second)
	m.ObserveMemory("add", nil)
	m.ObserveMemory("search", domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "down", nil))
	m.ObserveJob("memory_add", errors.New("boom"), 4*time.Second)
	m.ObserveJobExhausted("memory_add", "attempts")
	m.SetJobs(3, 1, 2, 90*time.Second)
	m.SetAuditQueue(7, 1024, 1024)
	m.AddAuditDropped("queue_full", 2)
	m.SetDBConns(2, 6)
	m.AddDBWait(1, 5*time.Millisecond)

	rec := httptest.NewRecorder()
	m.Handler().ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		`memosd_http_requests_total{method="POST",role="console",route="/api/dashboard/project/list",status="200"} 1`,
		`memosd_http_requests_total{method="GET",role="console",route="other",status="404"} 1`,
		`memosd_upstream_requests_total{endpoint="/product/search",outcome="ok"} 1`,
		`memosd_upstream_requests_total{endpoint="/product/get_memory/{id}",outcome="unavailable"} 1`,
		`memosd_memory_operations_total{op="search",result="unavailable"} 1`,
		`memosd_worker_jobs{state="pending"} 3`,
		`memosd_worker_jobs{state="failed"} 2`,
		`memosd_worker_jobs_exhausted_total{kind="memory_add",reason="attempts"} 1`,
		`memosd_worker_oldest_pending_seconds 90`,
		`memosd_audit_queue_depth 7`,
		`memosd_audit_queue_capacity 1024`,
		`memosd_audit_dropped_total{reason="queue_full"} 2`,
		`memosd_db_open_connections{state="in_use"} 2`,
		`memosd_build_info{role="all",version="test-version"} 1`,
		`go_goroutines`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %q", want)
		}
	}
}

func TestNilMetricsIsSafe(t *testing.T) {
	t.Parallel()

	var m *Metrics
	m.ObserveHTTP(HTTPObservation{Role: "console", Method: "GET", Route: "/", Status: 200, Duration: time.Millisecond})
	m.ObserveUpstream("/product/add", "ok", time.Millisecond)
	m.ObserveMemory("add", nil)
	m.ObserveJob("memory_add", nil, time.Millisecond)
	m.ObserveJobExhausted("memory_add", "permanent")
	m.SetJobs(1, 1, 1, time.Second)
	m.SetAuditQueue(1, 2, 1)
	m.AddAuditDropped("queue_full", 1)
	m.SetDBConns(1, 1)
	m.AddDBWait(1, time.Millisecond)
}

func TestResultOf(t *testing.T) {
	t.Parallel()

	cases := []struct {
		err  error
		want string
	}{
		{nil, "ok"},
		{domain.Invalid("x", "bad"), "invalid"},
		{domain.NotFound("x", "missing"), "not_found"},
		{domain.Wrap(domain.ErrForbidden, "x", "no", nil), "denied"},
		{domain.Wrap(domain.ErrUpstreamUnavailable, "x", "down", nil), "unavailable"},
		{domain.Wrap(domain.ErrUpstreamRejected, "x", "rejected", nil), "rejected"},
		{domain.Wrap(domain.ErrUpstreamDenied, "x", "denied", nil), "upstream_denied"},
		{errors.New("boom"), "error"},
	}
	for _, tc := range cases {
		if got := ResultOf(tc.err); got != tc.want {
			t.Errorf("ResultOf(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}
