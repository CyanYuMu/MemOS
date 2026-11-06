package config

import (
	"strings"
	"testing"
	"time"
)

func envFrom(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestFromEnvDefaults(t *testing.T) {
	t.Parallel()

	cfg, err := FromEnv(envFrom(nil))
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if cfg.Role != RoleAll {
		t.Errorf("role = %q, want all", cfg.Role)
	}
	if cfg.HTTP.ConsoleAddr != ":5280" || cfg.HTTP.MCPAddr != ":8002" {
		t.Errorf("addrs = %q/%q", cfg.HTTP.ConsoleAddr, cfg.HTTP.MCPAddr)
	}
	if cfg.Upstream.Timeout != 300*time.Second {
		t.Errorf("timeout = %v", cfg.Upstream.Timeout)
	}
	if cfg.Store.Path != "data/memosd.db" {
		t.Errorf("db path = %q", cfg.Store.Path)
	}
	if cfg.Store.MigrationTimeout != 10*time.Minute {
		t.Errorf("migration timeout = %v, want 10m", cfg.Store.MigrationTimeout)
	}
	if cfg.Identity.AdminToken != "" || len(cfg.Identity.TenantTokens) != 0 {
		t.Error("identity should be empty by default")
	}
	if !cfg.Worker.AddAsync || cfg.Worker.Concurrency != 4 {
		t.Errorf("worker defaults = %+v", cfg.Worker)
	}
	if cfg.Limits.DreamTopK != 2 || cfg.HTTP.ReadTimeout != 60*time.Second {
		t.Errorf("limits/http defaults = %+v %+v", cfg.Limits, cfg.HTTP)
	}
	if cfg.Auth.SeedUser != "admin" || cfg.Auth.SeedPassword != "" || cfg.Auth.SessionTTL != 12*time.Hour || !cfg.Auth.CookieSecure {
		t.Errorf("auth defaults = %+v", cfg.Auth)
	}
	if cfg.Auth.SessionMaxLifetime != 30*24*time.Hour {
		t.Errorf("session max lifetime = %v, want 720h", cfg.Auth.SessionMaxLifetime)
	}
	if cfg.Auth.ZeroTrust.Enabled || cfg.Auth.ZeroTrust.Required || !cfg.Auth.ZeroTrust.AutoCreate || !cfg.Auth.AllowPassword {
		t.Errorf("zerotrust defaults = %+v allowPassword=%v", cfg.Auth.ZeroTrust, cfg.Auth.AllowPassword)
	}
	if cfg.Log.MaxSizeMB != 1024 || cfg.Log.MaxAgeDays != 60 || cfg.Log.MaxBackups != 5 {
		t.Errorf("log defaults = %+v", cfg.Log)
	}
	if cfg.HTTP.PprofAddr != "" {
		t.Errorf("pprof should be disabled by default: %q", cfg.HTTP.PprofAddr)
	}
	if cfg.Chat.Dir != "" || cfg.Chat.PiBin != "pi" || cfg.Chat.MaxProcs != 12 ||
		cfg.Chat.IdleTTL != 15*time.Minute || cfg.Chat.MCPURL != "http://127.0.0.1:8002/mcp" ||
		cfg.Chat.ContextWindow != 131072 || cfg.Chat.MaxTokens != 16384 {
		t.Errorf("chat defaults = %+v", cfg.Chat)
	}
	if cfg.Prompt != (PromptConfig{TestBurst: 3, TestRefillPerMin: 6, GenerateBurst: 2, GenerateRefillPerMin: 4}) {
		t.Errorf("prompt limits defaults = %+v", cfg.Prompt)
	}
	if cfg.LocalSync.Enabled {
		t.Error("local sync should be disabled until the plugin ships")
	}
}

func TestFromEnvOverrides(t *testing.T) {
	t.Parallel()

	cfg, err := FromEnv(envFrom(map[string]string{
		"MEMOSD_ROLE":                 "mcp",
		"MEMOSD_CONSOLE_ADDR":         "127.0.0.1:7000",
		"MEMOSD_MCP_ADDR":             "127.0.0.1:7001",
		"MEMOS_API_BASE_URL":          "http://memos.example:8000/",
		"MEMOS_TIMEOUT":               "42s",
		"MEMOS_TENANTS":               `{"tok-a":{"user_id":"alice","cube_id":"cube-a"}}`,
		"MEMOSD_ADMIN_TOKEN":          "0123456789abcdef",
		"MEMOS_ADD_ASYNC":             "off",
		"MEMOSD_DREAM_TOP_K":          "3",
		"MEMORY_QUERY_PREFIX":         "prefix ",
		"MEMOSD_JOB_TIMEOUT":          "90s",
		"MEMOSD_READ_TIMEOUT":         "15s",
		"MEMOSD_MAX_BODY_BYTES":       "1024",
		"MEMOSD_MAX_TOP_K":            "20",
		"MEMOSD_DEFAULT_TOP_K":        "5",
		"MEMOSD_WORKER_POLL":          "500ms",
		"MEMOSD_WORKER_CONCURRENCY":   "8",
		"MEMOSD_AUDIT_RETENTION_DAYS": "7",
		"MEMOSD_PPROF_ADDR":           "127.0.0.1:6060",
		"MEMOSD_LOG_MAX_MB":           "2048",
		"MEMOSD_CHAT_DIR":             "/var/lib/memosd/chat",
		"MEMOSD_CHAT_PI_BIN":          "/usr/local/bin/pi",
		"MEMOSD_CHAT_MAX_PROCS":       "4",
		"MEMOSD_CHAT_IDLE_TTL":        "5m",
		"MEMOSD_CHAT_MCP_URL":         "http://127.0.0.1:9000/mcp",
		"MEMOSD_CHAT_CONTEXT_WINDOW":  "65536",
		"MEMOSD_CHAT_MAX_TOKENS":      "8192",
	}))
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if cfg.Role != RoleMCP {
		t.Errorf("role = %q", cfg.Role)
	}
	if cfg.Upstream.BaseURL != "http://memos.example:8000" {
		t.Errorf("base url = %q", cfg.Upstream.BaseURL)
	}
	if cfg.Upstream.Timeout != 42*time.Second {
		t.Errorf("timeout = %v", cfg.Upstream.Timeout)
	}
	if cfg.HTTP.PprofAddr != "127.0.0.1:6060" || cfg.Log.MaxSizeMB != 2048 {
		t.Errorf("pprof/log overrides = %q/%d", cfg.HTTP.PprofAddr, cfg.Log.MaxSizeMB)
	}
	if len(cfg.Identity.TenantTokens) != 1 || cfg.Identity.TenantTokens[0].UserID != "alice" ||
		cfg.Identity.TenantTokens[0].CubeID != "cube-a" {
		t.Errorf("tenants = %+v", cfg.Identity.TenantTokens)
	}
	if cfg.Worker.AddAsync {
		t.Errorf("worker = %+v", cfg.Worker)
	}
	if cfg.Limits.DreamTopK != 3 || cfg.Limits.QueryPrefix != "prefix" ||
		cfg.HTTP.ReadTimeout != 15*time.Second || cfg.Worker.JobTimeout != 90*time.Second ||
		cfg.Worker.Concurrency != 8 {
		t.Errorf("limits/http/worker = %+v %+v %+v", cfg.Limits, cfg.HTTP, cfg.Worker)
	}
	if cfg.HTTP.MaxBodyBytes != 1024 || cfg.HTTP.ShutdownTimeout <= 0 {
		t.Errorf("http = %+v", cfg.HTTP)
	}
	if cfg.Limits.DefaultTopK != 5 || cfg.Limits.MaxTopK != 20 {
		t.Errorf("limits = %+v", cfg.Limits)
	}
	if cfg.Audit.RetentionDays != 7 {
		t.Errorf("audit = %+v", cfg.Audit)
	}
	if cfg.Chat.Dir != "/var/lib/memosd/chat" || cfg.Chat.PiBin != "/usr/local/bin/pi" ||
		cfg.Chat.MaxProcs != 4 || cfg.Chat.IdleTTL != 5*time.Minute ||
		cfg.Chat.MCPURL != "http://127.0.0.1:9000/mcp" ||
		cfg.Chat.ContextWindow != 65536 || cfg.Chat.MaxTokens != 8192 {
		t.Errorf("chat = %+v", cfg.Chat)
	}
}

func TestFromEnvErrors(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		env  map[string]string
		want string
	}{
		{name: "bad role", env: map[string]string{"MEMOSD_ROLE": "api"}, want: "MEMOSD_ROLE"},
		{name: "bad timeout", env: map[string]string{"MEMOS_TIMEOUT": "soon"}, want: "MEMOS_TIMEOUT"},
		{name: "short timeout", env: map[string]string{"MEMOS_TIMEOUT": "500ms"}, want: "at least 1s"},
		{name: "bad default top k", env: map[string]string{"MEMOSD_DEFAULT_TOP_K": "0"}, want: "MEMOSD_DEFAULT_TOP_K"},
		{name: "bad dream top k", env: map[string]string{"MEMOSD_DREAM_TOP_K": "-1"}, want: "MEMOSD_DREAM_TOP_K"},
		{name: "bad read timeout", env: map[string]string{"MEMOSD_READ_TIMEOUT": "0s"}, want: "MEMOSD_READ_TIMEOUT"},
		{name: "bad job timeout", env: map[string]string{"MEMOSD_JOB_TIMEOUT": "0s"}, want: "MEMOSD_JOB_TIMEOUT"},
		{name: "bad worker concurrency", env: map[string]string{"MEMOSD_WORKER_CONCURRENCY": "0"}, want: "MEMOSD_WORKER_CONCURRENCY"},
		{name: "metrics addr collision", env: map[string]string{"MEMOSD_METRICS_ADDR": ":5280"}, want: "MEMOSD_METRICS_ADDR"},
		{name: "bad bool", env: map[string]string{"MEMOS_ADD_ASYNC": "maybe"}, want: "MEMOS_ADD_ASYNC"},
		{name: "wildcard cors", env: map[string]string{"MEMOSD_CORS_ORIGINS": "*"}, want: "CORS"},
		{name: "same addr", env: map[string]string{"MEMOSD_ROLE": "all", "MEMOSD_CONSOLE_ADDR": ":9000", "MEMOSD_MCP_ADDR": ":9000"}, want: "differ"},
		{name: "short admin token", env: map[string]string{"MEMOSD_ADMIN_TOKEN": "short"}, want: "MEMOSD_ADMIN_TOKEN"},
		{name: "bad upstream scheme", env: map[string]string{"MEMOS_API_BASE_URL": "memos:8000"}, want: "MEMOS_API_BASE_URL"},
		{name: "bad tenant json", env: map[string]string{"MEMOS_TENANTS": `{"tok-a":{}}`}, want: "user_id"},
		{name: "malformed tenant compact", env: map[string]string{"MEMOS_TENANTS": "tok-a"}, want: "malformed"},
		{name: "duplicate tenant", env: map[string]string{"MEMOS_TENANTS": "tok-a:a,tok-a:b"}, want: "duplicate"},
		{name: "short seed password", env: map[string]string{"MEMOSD_ADMIN_PASSWORD": "short"}, want: "MEMOSD_ADMIN_PASSWORD"},
		{name: "seed user too long", env: map[string]string{"MEMOSD_ADMIN_PASSWORD": "long-enough-1", "MEMOSD_ADMIN_USER": strings.Repeat("a", 65)}, want: "MEMOSD_ADMIN_USER"},
		{name: "short session ttl", env: map[string]string{"MEMOSD_SESSION_TTL": "30s"}, want: "MEMOSD_SESSION_TTL"},
		{name: "session max shorter than ttl", env: map[string]string{"MEMOSD_SESSION_TTL": "48h", "MEMOSD_SESSION_MAX_LIFETIME": "24h"}, want: "MEMOSD_SESSION_MAX_LIFETIME"},
		{name: "bad cookie secure", env: map[string]string{"MEMOSD_COOKIE_SECURE": "sometimes"}, want: "MEMOSD_COOKIE_SECURE"},
		{name: "bad chat procs", env: map[string]string{"MEMOSD_CHAT_MAX_PROCS": "0"}, want: "MEMOSD_CHAT_MAX_PROCS"},
		{name: "bad chat ttl", env: map[string]string{"MEMOSD_CHAT_IDLE_TTL": "0s"}, want: "MEMOSD_CHAT_IDLE_TTL"},
		{name: "bad chat mcp url", env: map[string]string{"MEMOSD_CHAT_MCP_URL": "memory-console/mcp"}, want: "MEMOSD_CHAT_MCP_URL"},
		{name: "bad chat context", env: map[string]string{"MEMOSD_CHAT_CONTEXT_WINDOW": "0"}, want: "MEMOSD_CHAT_CONTEXT_WINDOW"},
		{name: "bad chat max tokens", env: map[string]string{"MEMOSD_CHAT_MAX_TOKENS": "-1"}, want: "MEMOSD_CHAT_MAX_TOKENS"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()

			_, err := FromEnv(envFrom(tt.env))
			if err == nil {
				t.Fatal("FromEnv succeeded, want error")
			}
			if !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error = %q, want substring %q", err, tt.want)
			}
		})
	}
}

func TestParseTenantsCompact(t *testing.T) {
	t.Parallel()

	got, err := parseTenants("tok:with:colon:alice,tok-b:bob ,, tok-c:carol")
	if err != nil {
		t.Fatalf("parseTenants: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("len = %d, want 3", len(got))
	}
	if got[0].Token != "tok:with:colon" || got[0].UserID != "alice" {
		t.Errorf("first = %+v", got[0])
	}
	if got[1].CubeID != "bob" || got[2].CubeID != "carol" {
		t.Errorf("got = %+v", got)
	}
}

func TestRedactedHidesCredentials(t *testing.T) {
	t.Parallel()

	cfg, err := FromEnv(envFrom(map[string]string{
		"MEMOS_TENANTS":         "super-secret-token:alice",
		"MEMOSD_ADMIN_TOKEN":    "admin-secret-token-1234",
		"MEMOSD_ADMIN_PASSWORD": "seed-secret-pass-1",
		"MEMOS_API_BASE_URL":    "http://api-user:api-pass@memos.example:8000",
	}))
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if cfg.Upstream.BaseURL == "" {
		t.Fatal("base url missing")
	}
	out := cfg.Redacted()
	for _, secret := range []string{"super-secret-token", "admin-secret-token-1234", "seed-secret-pass-1", "api-user", "api-pass"} {
		if strings.Contains(out, secret) {
			t.Fatalf("redacted output leaks %q: %s", secret, out)
		}
	}
}

func TestFromEnvOperationalLimits(t *testing.T) {
	t.Parallel()

	cfg, err := FromEnv(envFrom(nil))
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if len(cfg.HTTP.CORSOrigins) != 0 {
		t.Errorf("CORS 默认应为空，got %v", cfg.HTTP.CORSOrigins)
	}
	if cfg.Upstream.DashboardMaxBytes != 512<<20 {
		t.Errorf("dashboard max = %d", cfg.Upstream.DashboardMaxBytes)
	}
	if cfg.Auth.MaxLoginFailures != 5 || cfg.Auth.LoginFailWindow != 10*time.Minute {
		t.Errorf("login throttle = %d/%v", cfg.Auth.MaxLoginFailures, cfg.Auth.LoginFailWindow)
	}
	if cfg.Limits.GraphMaxNodes != 4000 || cfg.Worker.MaxAttempts != 3 {
		t.Errorf("graph/attempts = %d/%d", cfg.Limits.GraphMaxNodes, cfg.Worker.MaxAttempts)
	}
	if cfg.Worker.RetryBase != 5*time.Second || cfg.Worker.RetryMax != 10*time.Minute {
		t.Errorf("retry backoff = %v/%v", cfg.Worker.RetryBase, cfg.Worker.RetryMax)
	}
	if cfg.Audit.QueueSize != 1024 || cfg.Audit.FlushInterval != time.Second {
		t.Errorf("audit buffer = %d/%v", cfg.Audit.QueueSize, cfg.Audit.FlushInterval)
	}

	cfg, err = FromEnv(envFrom(map[string]string{
		"MEMOSD_CORS_ORIGINS":                   "https://console.example",
		"MEMOSD_UPSTREAM_DASHBOARD_MAX_BYTES":   "1048576",
		"MEMOSD_LOGIN_MAX_FAILURES":             "9",
		"MEMOSD_LOGIN_FAIL_WINDOW":              "2m",
		"MEMOSD_SESSION_MAX_LIFETIME":           "48h",
		"MEMOSD_GRAPH_MAX_NODES":                "1500",
		"MEMOSD_JOB_MAX_ATTEMPTS":               "7",
		"MEMOSD_JOB_RETRY_BASE":                 "2s",
		"MEMOSD_JOB_RETRY_MAX":                  "30s",
		"MEMOSD_AUDIT_QUEUE_SIZE":               "16",
		"MEMOSD_AUDIT_FLUSH_INTERVAL":           "250ms",
		"MEMOSD_AUDIT_FILE":                     "/var/log/memory-console/audit.jsonl",
		"MEMOSD_AUDIT_FILE_MAX_MB":              "512",
		"MEMOSD_AUDIT_FILE_MAX_BACKUPS":         "0",
		"MEMOSD_AUDIT_FILE_MAX_AGE_DAYS":        "0",
		"MEMOSD_AUDIT_FILE_COMPRESS":            "false",
		"MEMOSD_MIGRATION_TIMEOUT":              "90m",
		"MEMOSD_PROMPT_TEST_BURST":              "5",
		"MEMOSD_PROMPT_TEST_REFILL_PER_MIN":     "9",
		"MEMOSD_PROMPT_GENERATE_BURST":          "4",
		"MEMOSD_PROMPT_GENERATE_REFILL_PER_MIN": "3",
	}))
	if err != nil {
		t.Fatalf("FromEnv overrides: %v", err)
	}
	if cfg.HTTP.CORSOrigins[0] != "https://console.example" || cfg.Upstream.DashboardMaxBytes != 1<<20 {
		t.Errorf("cors/dashboard = %v/%d", cfg.HTTP.CORSOrigins, cfg.Upstream.DashboardMaxBytes)
	}
	if cfg.Auth.MaxLoginFailures != 9 || cfg.Auth.LoginFailWindow != 2*time.Minute {
		t.Errorf("login throttle = %d/%v", cfg.Auth.MaxLoginFailures, cfg.Auth.LoginFailWindow)
	}
	if cfg.Auth.SessionMaxLifetime != 48*time.Hour {
		t.Errorf("session max lifetime = %v", cfg.Auth.SessionMaxLifetime)
	}
	if cfg.Limits.GraphMaxNodes != 1500 || cfg.Worker.MaxAttempts != 7 {
		t.Errorf("graph/attempts = %d/%d", cfg.Limits.GraphMaxNodes, cfg.Worker.MaxAttempts)
	}
	if cfg.Worker.RetryBase != 2*time.Second || cfg.Worker.RetryMax != 30*time.Second {
		t.Errorf("retry backoff = %v/%v", cfg.Worker.RetryBase, cfg.Worker.RetryMax)
	}
	if cfg.Audit.QueueSize != 16 || cfg.Audit.FlushInterval != 250*time.Millisecond {
		t.Errorf("audit buffer = %d/%v", cfg.Audit.QueueSize, cfg.Audit.FlushInterval)
	}
	if cfg.Audit.File != "/var/log/memory-console/audit.jsonl" ||
		cfg.Audit.FileMaxSizeMB != 512 || cfg.Audit.FileMaxBackups != 0 ||
		cfg.Audit.FileMaxAgeDays != 0 || cfg.Audit.FileCompress {
		t.Errorf("audit file = %+v", cfg.Audit)
	}
	if cfg.Store.MigrationTimeout != 90*time.Minute {
		t.Errorf("migration timeout = %v", cfg.Store.MigrationTimeout)
	}
	if cfg.Prompt != (PromptConfig{TestBurst: 5, TestRefillPerMin: 9, GenerateBurst: 4, GenerateRefillPerMin: 3}) {
		t.Errorf("prompt limits = %+v", cfg.Prompt)
	}
}

func TestFromEnvRejectsBadOperationalLimits(t *testing.T) {
	t.Parallel()

	for name, env := range map[string]map[string]string{
		"graph":      {"MEMOSD_GRAPH_MAX_NODES": "10001"},
		"login":      {"MEMOSD_LOGIN_MAX_FAILURES": "0"},
		"audit":      {"MEMOSD_AUDIT_FLUSH_INTERVAL": "10ms"},
		"attempt":    {"MEMOSD_JOB_MAX_ATTEMPTS": "0"},
		"retry_base": {"MEMOSD_JOB_RETRY_BASE": "0s"},
		"retry_max":  {"MEMOSD_JOB_RETRY_BASE": "10s", "MEMOSD_JOB_RETRY_MAX": "5s"},
		"migration":  {"MEMOSD_MIGRATION_TIMEOUT": "0s"},
		"prompt":     {"MEMOSD_PROMPT_TEST_BURST": "0"},
		"prompt_gen": {"MEMOSD_PROMPT_GENERATE_REFILL_PER_MIN": "0"},
	} {
		if _, err := FromEnv(envFrom(env)); err == nil {
			t.Errorf("%s: 期望校验失败", name)
		}
	}
}

func TestZeroTrustConfig(t *testing.T) {
	t.Parallel()

	ok := map[string]string{
		"MEMOSD_ZEROTRUST_ENABLED":       "true",
		"MEMOSD_ZEROTRUST_REQUIRED":      "true",
		"MEMOSD_AUTH_ALLOW_PASSWORD":     "false",
		"MEMOSD_ZEROTRUST_SECRET":        "0123456789abcdef",
		"MEMOSD_ZEROTRUST_ALLOWED_USERS": "alice, bob",
	}
	cfg, err := FromEnv(envFrom(ok))
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	zt := cfg.Auth.ZeroTrust
	if !zt.Enabled || !zt.Required || !zt.AutoCreate || len(zt.Secrets) != 1 || zt.Secrets[0] != "0123456789abcdef" || len(zt.AllowedUsers) != 2 {
		t.Fatalf("zerotrust = %+v", zt)
	}
	if cfg.Auth.AllowPassword {
		t.Fatal("AllowPassword 应为 false")
	}

	rotated, err := FromEnv(envFrom(map[string]string{
		"MEMOSD_ZEROTRUST_ENABLED": "true",
		"MEMOSD_ZEROTRUST_SECRET":  "0123456789abcdef, fedcba9876543210",
	}))
	if err != nil {
		t.Fatalf("rotated FromEnv: %v", err)
	}
	if got := rotated.Auth.ZeroTrust.Secrets; len(got) != 2 || got[1] != "fedcba9876543210" {
		t.Fatalf("rotated secrets = %#v", got)
	}

	for name, env := range map[string]map[string]string{
		"required without enabled": {"MEMOSD_ZEROTRUST_REQUIRED": "true", "MEMOSD_AUTH_ALLOW_PASSWORD": "false"},
		"required with password": {
			"MEMOSD_ZEROTRUST_ENABLED": "true", "MEMOSD_ZEROTRUST_REQUIRED": "true",
			"MEMOSD_ZEROTRUST_SECRET": "0123456789abcdef",
		},
		"short secret": {"MEMOSD_ZEROTRUST_ENABLED": "true", "MEMOSD_ZEROTRUST_SECRET": "short"},
		"short rotated secret": {
			"MEMOSD_ZEROTRUST_ENABLED": "true",
			"MEMOSD_ZEROTRUST_SECRET":  "0123456789abcdef,short",
		},
		"duplicate allow list": {
			"MEMOSD_ZEROTRUST_ENABLED": "true", "MEMOSD_ZEROTRUST_SECRET": "0123456789abcdef",
			"MEMOSD_ZEROTRUST_ALLOWED_USERS": "alice,Alice",
		},
	} {
		if _, err := FromEnv(envFrom(env)); err == nil {
			t.Errorf("%s: 期望校验失败", name)
		}
	}
}

func TestRedisConfig(t *testing.T) {
	t.Parallel()

	cfg, err := FromEnv(envFrom(nil))
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if cfg.Redis.URL != "" || cfg.Redis.Prefix != "memosd:" || cfg.Redis.Timeout != 500*time.Millisecond {
		t.Fatalf("redis defaults = %q/%q/%v", cfg.Redis.URL, cfg.Redis.Prefix, cfg.Redis.Timeout)
	}

	cfg, err = FromEnv(envFrom(map[string]string{
		"MEMOSD_REDIS_URL":     "redis://user:redis-secret@redis:6379/1",
		"MEMOSD_REDIS_PREFIX":  "console:",
		"MEMOSD_REDIS_TIMEOUT": "200ms",
	}))
	if err != nil {
		t.Fatalf("FromEnv overrides: %v", err)
	}
	if cfg.Redis.Prefix != "console:" || cfg.Redis.Timeout != 200*time.Millisecond {
		t.Fatalf("redis override = %q/%v", cfg.Redis.Prefix, cfg.Redis.Timeout)
	}
	redacted := cfg.Redacted()
	if !strings.Contains(redacted, "redis=redis://redis:6379/1") {
		t.Fatalf("redacted should keep host: %s", redacted)
	}
	for _, secret := range []string{"redis-secret", "user"} {
		if strings.Contains(redacted, secret) {
			t.Fatalf("redacted leaks %q: %s", secret, redacted)
		}
	}

	for name, env := range map[string]map[string]string{
		"bad scheme":   {"MEMOSD_REDIS_URL": "http://redis:6379"},
		"no host":      {"MEMOSD_REDIS_URL": "redis://"},
		"zero timeout": {"MEMOSD_REDIS_TIMEOUT": "0s"},
	} {
		if _, err := FromEnv(envFrom(env)); err == nil {
			t.Errorf("%s: want error", name)
		}
	}
}

func TestPublicIntegrationURLsArePairedAndValidated(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		service, console string
		valid            bool
	}{
		{"", "", true}, {"https://dev.example/", "https://console.example/", true}, {"https://dev.example", "", false}, {"https://u:p@dev.example", "https://console.example", false}, {"https://dev.example?token=hidden", "https://console.example", false},
	} {
		_, err := FromEnv(envFrom(map[string]string{"MEMOSD_PUBLIC_SERVICE_URL": tc.service, "MEMOSD_PUBLIC_CONSOLE_URL": tc.console}))
		if (err == nil) != tc.valid {
			t.Fatalf("valid=%v err=%v", tc.valid, err)
		}
	}
}

// TestFromEnvDecompressedFollowsBatchLimit 存量部署可能把单批上限调得比默认解压
// 上限更大；未显式设置新变量时解压上限应跟随单批上限，而不是让进程启动失败。
func TestFromEnvDecompressedFollowsBatchLimit(t *testing.T) {
	t.Parallel()

	cfg, err := FromEnv(envFrom(map[string]string{
		"MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES": "536870912", // 512 MiB
	}))
	if err != nil {
		t.Fatalf("FromEnv with larger batch limit: %v", err)
	}
	if cfg.LocalSync.MaxBatchDecompressed != cfg.LocalSync.MaxBatchBytes {
		t.Fatalf("decompressed = %d, batch = %d, want equal",
			cfg.LocalSync.MaxBatchDecompressed, cfg.LocalSync.MaxBatchBytes)
	}

	// 显式设置且小于单批上限仍然报错，避免用户以为解压上限比压缩上限大。
	if _, err := FromEnv(envFrom(map[string]string{
		"MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES":        "536870912",
		"MEMOSD_LOCAL_SYNC_MAX_BATCH_DECOMPRESSED": "134217728",
	})); err == nil {
		t.Fatal("explicit smaller decompressed limit should be rejected")
	}
}
