package httpx

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/metrics"
)

type capturedRequest struct {
	role, method, route string
	status              int
}

type captureObserver struct{ got []capturedRequest }

func (c *captureObserver) ObserveHTTP(in metrics.HTTPObservation) {
	c.got = append(c.got, capturedRequest{role: in.Role, method: in.Method, route: in.Route, status: in.Status})
}

func TestAccessLogUsesRouteTemplate(t *testing.T) {
	t.Parallel()

	obs := &captureObserver{}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/items/{id}/revoke", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	h := AccessLog(slog.New(slog.DiscardHandler),
		AccessLogOptions{Role: "console", Metrics: obs})(mux)

	ctx := context.Background()
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequestWithContext(ctx, http.MethodPost, "/api/items/abc/revoke", nil))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequestWithContext(ctx, http.MethodGet, "/not-registered/123", nil))

	if len(obs.got) != 2 {
		t.Fatalf("observations = %+v", obs.got)
	}
	if got := obs.got[0]; got.role != "console" || got.method != "POST" ||
		got.route != "/api/items/{id}/revoke" || got.status != http.StatusNoContent {
		t.Fatalf("matched route = %+v", got)
	}
	if got := obs.got[1]; got.route != "other" || got.status != http.StatusNotFound {
		t.Fatalf("unmatched route = %+v", got)
	}
}
