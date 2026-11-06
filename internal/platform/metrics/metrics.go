package metrics

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Options 是进程指标的构造参数。
type Options struct {
	Version string
	Role    string
}

// Metrics 持有本进程的 Prometheus 指标。所有记录方法都对 nil 安全，
// 未启用指标的调用方可以传 nil，不必到处判空。
type Metrics struct {
	reg *prometheus.Registry

	httpRequests    *prometheus.CounterVec
	httpDuration    *prometheus.HistogramVec
	upstreamCalls   *prometheus.CounterVec
	upstreamTime    *prometheus.HistogramVec
	memoryOps       *prometheus.CounterVec
	workerJobs      *prometheus.GaugeVec
	workerOldest    prometheus.Gauge
	workerDuration  *prometheus.HistogramVec
	workerExhausted *prometheus.CounterVec
	auditQueue      prometheus.Gauge
	auditCapacity   prometheus.Gauge
	auditBytes      prometheus.Gauge
	auditDropped    *prometheus.CounterVec
	auditWritten    prometheus.Counter
	auditFailed     prometheus.Counter
	auditFileFailed prometheus.Counter
	auditLastWrite  prometheus.Gauge
	dbConns         *prometheus.GaugeVec
	dbWaitTotal     prometheus.Counter
	dbWaitSeconds   prometheus.Counter
	logWriteErrors  *prometheus.CounterVec
	chatSlots       *prometheus.GaugeVec
	chatEvictions   prometheus.Counter
	chatBusy        prometheus.Counter
	chatAdmission   prometheus.Histogram
	chatPiRSS       prometheus.Gauge
}

const (
	bucketSlow = 300 // 上游搜索 p99 实测可达 157s
)

var durationBuckets = []float64{
	.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 180, bucketSlow,
}

var jobBuckets = []float64{.1, .5, 1, 2.5, 5, 10, 30, 60, 120, 300}

// New 构造指标集并注册 Go runtime 与进程采集器。
func New(opts Options) *Metrics {
	reg := prometheus.NewRegistry()
	m := &Metrics{
		reg: reg,
		httpRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "memosd_http_requests_total",
			Help: "Inbound HTTP requests by role, route template and status.",
		}, []string{"role", "method", "route", "status"}),
		httpDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "memosd_http_request_duration_seconds",
			Help:    "Inbound HTTP request latency by role and route template.",
			Buckets: durationBuckets,
		}, []string{"role", "method", "route"}),
		upstreamCalls: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "memosd_upstream_requests_total",
			Help: "memos-api calls by endpoint and outcome.",
		}, []string{"endpoint", "outcome"}),
		upstreamTime: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "memosd_upstream_request_duration_seconds",
			Help:    "memos-api call latency by endpoint.",
			Buckets: durationBuckets,
		}, []string{"endpoint"}),
		memoryOps: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "memosd_memory_operations_total",
			Help: "Memory use cases by operation and result.",
		}, []string{"op", "result"}),
		workerJobs: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "memosd_worker_jobs",
			Help: "Asynchronous jobs by state.",
		}, []string{"state"}),
		workerOldest: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "memosd_worker_oldest_pending_seconds",
			Help: "Age of the oldest pending asynchronous job.",
		}),
		workerDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "memosd_worker_job_duration_seconds",
			Help:    "Asynchronous job execution time by kind and result.",
			Buckets: jobBuckets,
		}, []string{"kind", "result"}),
		workerExhausted: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "memosd_worker_jobs_exhausted_total",
			Help: "Asynchronous jobs that reached a permanent failure, by kind and reason.",
		}, []string{"kind", "reason"}),
		auditQueue: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "memosd_audit_queue_depth",
			Help: "Audit records waiting in the in-process queue.",
		}),
		auditBytes: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "memosd_audit_queue_bytes",
			Help: "Estimated memory held by queued audit records.",
		}),
		auditCapacity: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "memosd_audit_queue_capacity",
			Help: "Configured audit queue capacity in records.",
		}),
		auditDropped: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "memosd_audit_dropped_total",
			Help: "Audit records dropped before reaching SQLite.",
		}, []string{"reason"}),
		auditWritten: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "memosd_audit_written_total",
			Help: "Audit records persisted to SQLite.",
		}),
		auditFailed: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "memosd_audit_write_failed_total",
			Help: "Audit records that failed to persist to SQLite.",
		}),
		auditFileFailed: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "memosd_audit_file_write_failed_total",
			Help: "Audit archive JSONL lines that failed to write.",
		}),
		auditLastWrite: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "memosd_audit_last_write_timestamp_seconds",
			Help: "Unix time of the last successful audit write.",
		}),
		dbConns: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "memosd_db_open_connections",
			Help: "SQLite connections by state.",
		}, []string{"state"}),
		dbWaitTotal: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "memosd_db_wait_count_total",
			Help: "Total number of waits for a SQLite connection.",
		}),
		dbWaitSeconds: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "memosd_db_wait_duration_seconds_total",
			Help: "Total time blocked waiting for a SQLite connection.",
		}),
		logWriteErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "memosd_log_write_errors_total",
			Help: "Failures writing or flushing the log sink, by operation.",
		}, []string{"op"}),
	}
	m.initChatMetrics()
	reg.MustRegister(
		m.httpRequests, m.httpDuration,
		m.upstreamCalls, m.upstreamTime,
		m.memoryOps,
		m.workerJobs, m.workerOldest, m.workerDuration, m.workerExhausted,
		m.auditQueue, m.auditBytes, m.auditDropped,
		m.auditWritten, m.auditFailed, m.auditFileFailed, m.auditLastWrite,
		m.auditCapacity,
		m.dbConns, m.dbWaitTotal, m.dbWaitSeconds,
		m.logWriteErrors,
		m.chatSlots, m.chatEvictions, m.chatBusy, m.chatAdmission, m.chatPiRSS,
		prometheus.NewGaugeFunc(prometheus.GaugeOpts{
			Name: "memosd_build_info",
			Help: "Build and role information; always 1.",
			ConstLabels: prometheus.Labels{
				"version": opts.Version,
				"role":    opts.Role,
			},
		}, func() float64 { return 1 }),
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	return m
}

func (m *Metrics) initChatMetrics() {
	m.chatSlots = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "memosd_chat_slots",
		Help: "Chat agent process slots by state.",
	}, []string{"state"})
	m.chatEvictions = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "memosd_chat_evictions_total",
		Help: "Idle chat sessions evicted to admit a new one.",
	})
	m.chatBusy = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "memosd_chat_busy_total",
		Help: "Chat admissions rejected because every slot was generating.",
	})
	m.chatAdmission = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "memosd_chat_admission_seconds",
		Help:    "Time spent admitting a chat agent process.",
		Buckets: []float64{.01, .05, .1, .25, .5, 1, 2.5, 5, 10},
	})
	m.chatPiRSS = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "memosd_chat_pi_rss_bytes",
		Help: "Resident memory of all running pi chat processes.",
	})
}

// Handler 返回 /metrics 的处理器。
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.reg, promhttp.HandlerOpts{EnableOpenMetrics: true})
}

// HTTPObservation 是一次入站请求的观测入参；Route 用路由模板，未知路径为 "other"。
type HTTPObservation struct {
	Role     string
	Method   string
	Route    string
	Status   int
	Duration time.Duration
}

// ObserveHTTP 记录一次入站请求。
func (m *Metrics) ObserveHTTP(in HTTPObservation) {
	if m == nil {
		return
	}
	route := in.Route
	if route == "" {
		route = "other"
	}
	m.httpRequests.WithLabelValues(in.Role, in.Method, route, strconv.Itoa(in.Status)).Inc()
	m.httpDuration.WithLabelValues(in.Role, in.Method, route).Observe(in.Duration.Seconds())
}

// ObserveUpstream 记录一次 memos-api 调用。outcome 见 OutcomeOf。
func (m *Metrics) ObserveUpstream(endpoint, outcome string, d time.Duration) {
	if m == nil {
		return
	}
	m.upstreamCalls.WithLabelValues(endpoint, outcome).Inc()
	m.upstreamTime.WithLabelValues(endpoint).Observe(d.Seconds())
}

// ObserveMemory 记录一次记忆用例的最终结果。
func (m *Metrics) ObserveMemory(op string, err error) {
	if m == nil {
		return
	}
	m.memoryOps.WithLabelValues(op, ResultOf(err)).Inc()
}

// ObserveJob 记录一次异步任务的执行结果与耗时。
func (m *Metrics) ObserveJob(kind string, err error, d time.Duration) {
	if m == nil {
		return
	}
	m.workerDuration.WithLabelValues(kind, ResultOf(err)).Observe(d.Seconds())
}

// ObserveJobExhausted 记录一次进入终态失败的任务。
func (m *Metrics) ObserveJobExhausted(kind, reason string) {
	if m == nil {
		return
	}
	m.workerExhausted.WithLabelValues(kind, reason).Inc()
}

// SetJobs 更新任务积压指标。
func (m *Metrics) SetJobs(pending, running, failed int64, oldest time.Duration) {
	if m == nil {
		return
	}
	m.workerJobs.WithLabelValues("pending").Set(float64(pending))
	m.workerJobs.WithLabelValues("running").Set(float64(running))
	m.workerJobs.WithLabelValues("failed").Set(float64(failed))
	seconds := oldest.Seconds()
	if seconds < 0 {
		seconds = 0
	}
	m.workerOldest.Set(seconds)
}

// SetAuditQueue 更新审计队列深度、容量与占用字节。
func (m *Metrics) SetAuditQueue(depth, capacity int, bytes int64) {
	if m == nil {
		return
	}
	m.auditQueue.Set(float64(depth))
	m.auditCapacity.Set(float64(capacity))
	m.auditBytes.Set(float64(bytes))
}

// AddAuditDropped 累加审计丢弃数。reason 只允许 queue_full / byte_budget。
func (m *Metrics) AddAuditDropped(reason string, n int64) {
	if m == nil || n <= 0 {
		return
	}
	m.auditDropped.WithLabelValues(reason).Add(float64(n))
}

// SetAuditProgress 更新审计落库计数与最近成功时间，用来判断审计是否还在产出。
func (m *Metrics) SetAuditProgress(written, failed int64, lastWrite time.Time) {
	if m == nil {
		return
	}
	m.auditWritten.Add(float64(written))
	m.auditFailed.Add(float64(failed))
	if !lastWrite.IsZero() {
		m.auditLastWrite.Set(float64(lastWrite.Unix()))
	}
}

// AddAuditFileFailed 累加审计归档文件写入失败数。
func (m *Metrics) AddAuditFileFailed(n int64) {
	if m == nil || n <= 0 {
		return
	}
	m.auditFileFailed.Add(float64(n))
}

// SetDBConns 更新 SQLite 连接池状态。
func (m *Metrics) SetDBConns(inUse, idle int) {
	if m == nil {
		return
	}
	m.dbConns.WithLabelValues("in_use").Set(float64(inUse))
	m.dbConns.WithLabelValues("idle").Set(float64(idle))
}

// AddDBWait 累加连接池等待次数与时长（传自增差值）。
func (m *Metrics) AddDBWait(count int64, d time.Duration) {
	if m == nil {
		return
	}
	if count > 0 {
		m.dbWaitTotal.Add(float64(count))
	}
	if d > 0 {
		m.dbWaitSeconds.Add(d.Seconds())
	}
}

// ObserveLogError 记录一次日志落盘失败。op 只允许 write / flush。
func (m *Metrics) ObserveLogError(op string) {
	if m == nil {
		return
	}
	m.logWriteErrors.WithLabelValues(op).Inc()
}

// ResultOf 把领域错误收敛成低基数结果标签。
func ResultOf(err error) string {
	if err == nil {
		return "ok"
	}
	switch {
	case errors.Is(err, domain.ErrInvalidInput):
		return "invalid"
	case errors.Is(err, domain.ErrNotFound):
		return "not_found"
	case errors.Is(err, domain.ErrUnauthorized), errors.Is(err, domain.ErrForbidden):
		return "denied"
	case errors.Is(err, domain.ErrUpstreamUnavailable):
		return "unavailable"
	case errors.Is(err, domain.ErrUpstreamDenied):
		return "upstream_denied"
	case errors.Is(err, domain.ErrUpstreamRejected):
		return "rejected"
	case errors.Is(err, domain.ErrRateLimited):
		return "rate_limited"
	case errors.Is(err, domain.ErrDisabled):
		return "disabled"
	default:
		return "error"
	}
}

// SetChatSlots 更新对话进程槽位分布。total 包含 starting 与 stopping。
func (m *Metrics) SetChatSlots(total, running, idle, stopping int) {
	if m == nil {
		return
	}
	m.chatSlots.WithLabelValues("total").Set(float64(total))
	m.chatSlots.WithLabelValues("running").Set(float64(running))
	m.chatSlots.WithLabelValues("idle").Set(float64(idle))
	m.chatSlots.WithLabelValues("stopping").Set(float64(stopping))
}

// AddChatEviction 记录一次为腾出槽位而驱逐空闲会话。
func (m *Metrics) AddChatEviction() {
	if m == nil {
		return
	}
	m.chatEvictions.Inc()
}

// AddChatBusy 记录一次「所有槽位都在生成」导致的拒绝。
func (m *Metrics) AddChatBusy() {
	if m == nil {
		return
	}
	m.chatBusy.Inc()
}

// ObserveChatAdmission 记录一次准入耗时（含驱逐与等待）。
func (m *Metrics) ObserveChatAdmission(d time.Duration) {
	if m == nil {
		return
	}
	m.chatAdmission.Observe(d.Seconds())
}

// SetChatPiRSS 上报所有在跑 pi 进程的常驻内存合计。
func (m *Metrics) SetChatPiRSS(bytes int64) {
	if m == nil {
		return
	}
	m.chatPiRSS.Set(float64(bytes))
}
