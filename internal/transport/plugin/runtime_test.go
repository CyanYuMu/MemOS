package plugin

import (
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"
)

func TestInstallerUsesDeploymentURLsAndNoProductionFallback(t *testing.T) {
	t.Parallel()
	base := newServer(t)
	for _, host := range []string{"https://dev-gateway.example", "https://prod-gateway.example"} {
		s := New(base.root, Options{ServiceURL: host, ConsoleURL: host + "/console"})
		rec := httptest.NewRecorder()
		s.Handler().ServeHTTP(rec, httptest.NewRequestWithContext(t.Context(), "GET", "/memory/plugin/graphmemory/hermes_plugin_install.sh", nil))
		if rec.Code != 200 || !strings.Contains(rec.Body.String(), host+"/memory/plugin/graphmemory") || !strings.Contains(rec.Body.String(), host+"/console/cn/apikeys") {
			t.Fatalf("runtime installer: %d %s", rec.Code, rec.Body.String())
		}
	}
	rec := httptest.NewRecorder()
	New(base.root).Handler().ServeHTTP(rec, httptest.NewRequestWithContext(t.Context(), "GET", "/memory/plugin/graphmemory/hermes_plugin_install.sh", nil))
	if rec.Code != 503 {
		t.Fatalf("missing deployment addresses: %d", rec.Code)
	}
}

func TestInstallerShellDefaultsPreserveExplicitOverride(t *testing.T) {
	t.Parallel()
	base := newServer(t)
	service := "https://dev.example/quote'$(printf unsafe)"
	s := New(base.root, Options{ServiceURL: service, ConsoleURL: "https://console.example"})
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequestWithContext(t.Context(), "GET", "/memory/plugin/graphmemory/hermes_plugin_install.sh", nil))
	command := exec.CommandContext(t.Context(), "bash", "-s")
	command.Env = []string{"GRAPH_MEMORY_GATEWAY_URL=https://override.example"}
	command.Stdin = strings.NewReader(rec.Body.String() + `printf '%s\n' "$GRAPH_MEMORY_PLUGIN_BASE" "$GRAPH_MEMORY_GATEWAY_URL" "$GRAPH_MEMORY_KEYS_URL"`)
	out, err := command.CombinedOutput()
	want := service + "/memory/plugin/graphmemory\nhttps://override.example\nhttps://console.example/cn/apikeys\n"
	if err != nil || string(out) != want {
		t.Fatalf("shell defaults: %v %q", err, out)
	}
}
