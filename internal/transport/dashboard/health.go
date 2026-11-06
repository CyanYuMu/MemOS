package dashboard

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// healthProbeTimeout 是单轮健康检查的上限。页面每 30s 轮询一次，宁可快速报「不可用」，
// 也不要让上游故障把一次请求拖到分钟级。
const healthProbeTimeout = 3 * time.Second

// healthCheck 是前端 HealthCheck 契约：失败原因只给机器可读短码，文案由页面按语言翻译。
type healthCheck struct {
	OK        bool   `json:"ok"`
	LatencyMs int64  `json:"latencyMs"`
	Detail    string `json:"detail,omitempty"`
}

// health 返回网关与上游的真实探测结果。两边互不依赖，必须并发：
// 串行时最坏要等两个超时，页面上的「正在检查」会一直转。
func (s *Server) health(ctx context.Context, _ domain.Principal, _ *http.Request, _ map[string]any) (any, error) {
	probeCtx, cancel := context.WithTimeout(ctx, healthProbeTimeout)
	defer cancel()

	var (
		wg                sync.WaitGroup
		gateway, upstream healthCheck
	)
	wg.Add(2)
	go func() {
		defer wg.Done()
		gateway = s.probeGateway(probeCtx)
	}()
	go func() {
		defer wg.Done()
		upstream = s.probeUpstream(probeCtx)
	}()
	wg.Wait()

	status := "ok"
	if !gateway.OK && !upstream.OK {
		status = "down"
	} else if !gateway.OK || !upstream.OK {
		status = "degraded"
	}
	return map[string]any{
		"status":    status,
		"gateway":   gateway,
		"upstream":  upstream,
		"checkedAt": time.Now().UTC().Format(time.RFC3339),
	}, nil
}

// probeGateway 走真实的 HTTP 探测同进程的 MCP 入口，而不是假定「进程活着网关就活着」。
func (s *Server) probeGateway(ctx context.Context) healthCheck {
	started := time.Now()
	if s.deps.GatewayURL == "" {
		return healthCheck{Detail: "unreachable"}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		strings.TrimRight(s.deps.GatewayURL, "/")+"/healthz", nil)
	if err != nil {
		return healthCheck{LatencyMs: time.Since(started).Milliseconds(), Detail: "unreachable"}
	}
	resp, err := s.healthHTTP.Do(req)
	if err != nil {
		return healthCheck{LatencyMs: time.Since(started).Milliseconds(), Detail: probeDetail(err)}
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	latency := time.Since(started).Milliseconds()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return healthCheck{LatencyMs: latency, Detail: "http_" + strconv.Itoa(resp.StatusCode)}
	}
	return healthCheck{OK: true, LatencyMs: latency}
}

func (s *Server) probeUpstream(ctx context.Context) healthCheck {
	started := time.Now()
	if s.deps.Memories == nil {
		return healthCheck{Detail: "unreachable"}
	}
	_, err := s.deps.Memories.Health(ctx)
	latency := time.Since(started).Milliseconds()
	if err != nil {
		return healthCheck{LatencyMs: latency, Detail: probeDetail(err)}
	}
	return healthCheck{OK: true, LatencyMs: latency}
}

// probeDetail 把错误收敛成契约里的短码；具体错误只进日志，不进响应。
func probeDetail(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "timeout"
	}
	return "unreachable"
}
