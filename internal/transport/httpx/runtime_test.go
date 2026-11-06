package httpx

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRuntimeConfigIncludesOnlyPublicAddressesAndNeverCaches(t *testing.T) {
	t.Parallel()
	for _, service := range []string{"", "https://dev.example", "https://prod.example"} {
		rec := httptest.NewRecorder()
		PublicRuntimeConfig(service, "https://console.example").ServeHTTP(rec, httptest.NewRequestWithContext(t.Context(), "GET", "/runtime-config.js", nil))
		if rec.Code != 200 || rec.Header().Get("Cache-Control") != "no-store" || !strings.Contains(rec.Body.String(), `"serviceBase":"`+service+`"`) {
			t.Fatalf("config: %d %s", rec.Code, rec.Body.String())
		}
	}
}
