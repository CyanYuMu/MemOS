package app

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/pprof"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/piagent"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/rediscache"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/upstream"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/metrics"
	platformthrottle "github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/throttle"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/version"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/zerotrust"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/account"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	chatservice "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/chat"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/jobs"
	localsyncsvc "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/localsync"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/onboarding"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/prompt"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
	adminTransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/admin"
	authTransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
	chatTransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/chat"
	dashboardTransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/dashboard"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
	localsyncTransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/localsync"
	mcptransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/mcp"
	plugintransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/plugin"
	resttransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/rest"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/worker"
)

// Options 是组合根的输入。New 只构造，不监听端口。
type Options struct {
	Config config.Config
	Logger *slog.Logger
	// Metrics 可为空：入口进程先构造以接住日志落盘错误，测试与旧调用方不传时自行创建。
	Metrics *metrics.Metrics
}

// App 持有按角色构造的 server 与共享依赖。
type App struct {
	cfg       config.Config
	log       *slog.Logger
	ready     *httpx.Readiness
	store     *store.Store
	identity  *identity.Service
	accounts  *account.Service
	tenants   *tenant.Service
	zerotrust *zerotrust.Verifier
	auth      *authTransport.Server
	audit     *audit.Recorder
	redis     *rediscache.Conn
	memory    *memory.Service
	mcp       *mcptransport.Server
	rest      *resttransport.Server
	plugin    *plugintransport.Server
	admin     *adminTransport.Server
	dashboard *dashboardTransport.Server
	piagent   *piagent.Runtime
	chat      *chatTransport.Server
	localSync *localsyncTransport.Server
	metrics   *metrics.Metrics
	web       http.Handler
	worker    *worker.Service
	servers   []namedServer
}

type namedServer struct {
	name string
	srv  *http.Server
}

// 编译期确认 SQLite 实现满足 service 声明的聚合端口。
var _ service.Repository = (*store.Store)(nil)

// New 构造应用。配置已由调用方校验；这里只做装配期的交叉检查。
func New(opts Options) (*App, error) {
	if opts.Logger == nil {
		return nil, errors.New("app: logger is required")
	}
	cfg := opts.Config

	// 装配期的两个预算都由 buildBase 管：迁移用 Store.MigrationTimeout，
	// 其余初始化用 assembleTimeout。这里不设 deadline，避免大库迁移耗时
	// 把后续 Bootstrap 的 ctx 也一起耗掉。
	a, err := buildBase(context.Background(), cfg, opts.Logger)
	if err != nil {
		return nil, err
	}
	a.metrics = metricsOrNew(opts, cfg)

	client, err := upstream.New(upstream.Options{
		BaseURL:           cfg.Upstream.BaseURL,
		Timeout:           cfg.Upstream.Timeout,
		MaxResponseBytes:  cfg.Upstream.MaxResponseBytes,
		DashboardMaxBytes: cfg.Upstream.DashboardMaxBytes,
		Metrics:           a.metrics,
	}, opts.Logger)
	if err != nil {
		_ = a.store.Close()
		return nil, fmt.Errorf("app: build upstream client: %w", err)
	}
	// 契约自检：上游没实现 /product/contract 或版本不一致时 /readyz 返回 503，
	// 用旧镜像的上游会被挡在流量之外，而不是让控制面按旧口径静默算错。
	a.ready.Add("upstream", client.CheckContract)
	memoryOpts := memory.Options{
		DefaultTopK: cfg.Limits.DefaultTopK,
		MaxTopK:     cfg.Limits.MaxTopK,
		DreamTopK:   cfg.Limits.DreamTopK,
		QueryPrefix: cfg.Limits.QueryPrefix,
		AddAsync:    cfg.Worker.AddAsync,
		Idem:        a.store,
		Events:      a.store,
		Observer:    a.metrics,
	}
	a.memory = memory.New(client, a.store, memoryOpts, opts.Logger)
	localSyncSvc, dashboardLocalSync, err := a.buildLocalSync(cfg, opts.Logger)
	if err != nil {
		_ = a.store.Close()
		return nil, err
	}
	var localSyncMaintenance worker.LocalSyncMaintainer
	if cfg.LocalSync.Enabled {
		localSyncMaintenance = localSyncSvc
	}
	a.worker = worker.New(a.store, a.memory, opts.Logger, worker.Options{
		PollInterval:   cfg.Worker.PollInterval,
		JobTimeout:     cfg.Worker.JobTimeout,
		MaxAttempts:    cfg.Worker.MaxAttempts,
		RetryBase:      cfg.Worker.RetryBase,
		RetryMax:       cfg.Worker.RetryMax,
		Concurrency:    cfg.Worker.Concurrency,
		RetentionDays:  cfg.Worker.RetentionDays,
		IdempotencyTTL: cfg.Worker.IdempotencyTTL,
		Audit:          a.audit,
		Observer:       a.metrics,
		LocalPromoter:  localSyncSvc,
		LocalSync:      localSyncMaintenance,
	})

	if cfg.Role == config.RoleConsole || cfg.Role == config.RoleAll {
		if err := a.buildConsole(cfg, client, dashboardLocalSync); err != nil {
			_ = a.store.Close()
			return nil, err
		}
	}

	a.admin = adminTransport.New(adminTransport.Deps{
		Tenants:  a.tenants,
		Jobs:     jobs.New(a.store),
		Identity: a.identity,
		Accounts: a.accounts,
		Audit:    a.audit,
		Logger:   opts.Logger,
	})

	if cfg.Role == config.RoleMCP || cfg.Role == config.RoleAll {
		mcpSrv, mcpErr := mcptransport.New(mcptransport.Deps{
			Memory:   a.memory,
			Identity: a.identity,
			Audit:    a.audit,
			Logger:   opts.Logger,
			Name:     cfg.ServiceName,
			Version:  version.Version,
			Limits:   memoryOpts,
		})
		if mcpErr != nil {
			_ = a.store.Close()
			return nil, fmt.Errorf("app: build mcp: %w", mcpErr)
		}
		a.mcp = mcpSrv
		a.rest = resttransport.New(resttransport.Deps{
			Memory:   a.memory,
			Identity: a.identity,
			Tenants:  a.tenants,
			Audit:    a.audit,
			Logger:   opts.Logger,
			Limits:   memoryOpts,
		})
		a.plugin = plugintransport.New(cfg.HTTP.PluginDir, plugintransport.Options{ServiceURL: cfg.HTTP.PublicServiceURL, ConsoleURL: cfg.HTTP.PublicConsoleURL})
	}

	if err := a.buildServers(cfg); err != nil {
		_ = a.store.Close()
		return nil, err
	}
	return a, nil
}

// buildServers 按角色挂载对外监听；一个都没挂上说明角色值非法。
func (a *App) buildServers(cfg config.Config) error {
	if cfg.Role == config.RoleConsole || cfg.Role == config.RoleAll {
		h := a.consoleHandler()
		h = withRuntimeConfig(h, cfg.HTTP.PublicServiceURL, cfg.HTTP.PublicConsoleURL)
		a.servers = append(a.servers, namedServer{
			name: "console",
			srv:  httpx.NewServer(cfg.HTTP.ConsoleAddr, h, serverOptions(cfg)),
		})
	}
	if cfg.Role == config.RoleMCP || cfg.Role == config.RoleAll {
		h := a.mcpHandler()
		a.servers = append(a.servers, namedServer{
			name: "mcp",
			srv:  httpx.NewServer(cfg.HTTP.MCPAddr, h, serverOptions(cfg)),
		})
	}
	if cfg.HTTP.PprofAddr != "" {
		a.servers = append(a.servers, namedServer{
			name: "pprof",
			srv:  httpx.NewServer(cfg.HTTP.PprofAddr, pprofHandler(), serverOptions(cfg)),
		})
	}
	if cfg.HTTP.MetricsAddr != "" {
		a.servers = append(a.servers, namedServer{
			name: "metrics",
			srv:  httpx.NewServer(cfg.HTTP.MetricsAddr, metricsHandler(a.metrics), serverOptions(cfg)),
		})
	}
	if len(a.servers) == 0 {
		return fmt.Errorf("app: unsupported role %q", cfg.Role)
	}
	return nil
}

// NewMetrics 在构造日志器之前创建指标集。cmd/memosd 依赖不到 platform/metrics
// （depguard 只允许 cmd 依赖 app），所以由这里转出：日志落盘失败也要能累加到 /metrics。
func NewMetrics(cfg config.Config) *metrics.Metrics {
	return metrics.New(metrics.Options{Version: version.Version, Role: string(cfg.Role)})
}

// metricsOrNew 复用调用方传入的指标集；测试与只关心指标的调用方可以不给。
func metricsOrNew(opts Options, cfg config.Config) *metrics.Metrics {
	if opts.Metrics != nil {
		return opts.Metrics
	}
	return NewMetrics(cfg)
}

// buildBase 打开存储并构造身份、账号、审计与零信任校验器；失败时关闭存储。
func buildBase(ctx context.Context, cfg config.Config, log *slog.Logger) (*App, error) {
	// 迁移会重写全表时间列、建索引，不能被 30s 装配超时误杀：超时后事务回滚，
	// 下次启动重跑同一段。用 WithoutCancel 剥离父 ctx 的 deadline，再给一个
	// 独立可配置的预算。SIGTERM 仍会终止进程，未提交的事务由 SQLite 回滚，
	// 下次启动重跑。
	migrateTimeout := cfg.Store.MigrationTimeout
	if migrateTimeout <= 0 {
		migrateTimeout = defaultMigrationTimeout
	}
	migrateCtx, cancelMigrate := context.WithTimeout(context.WithoutCancel(ctx), migrateTimeout)
	defer cancelMigrate()
	st, err := store.Open(migrateCtx, cfg.Store.Path)
	if err != nil {
		return nil, fmt.Errorf("app: open store: %w", err)
	}

	// 大库迁移可能已经用掉整个装配预算；后面的 Bootstrap/节流/账号初始化
	// 必须拿新的窗口，否则会立刻拿到 context deadline exceeded，看起来像
	// 身份数据错误而不是超时。
	buildCtx, cancelBuild := context.WithTimeout(context.WithoutCancel(ctx), assembleTimeout)
	defer cancelBuild()

	var throttle throttleSetup
	fail := func(err error) (*App, error) {
		closeQuietly(throttle.redis)
		_ = st.Close()
		return nil, err
	}

	ids, err := buildIdentity(buildCtx, cfg, st, log)
	if err != nil {
		return fail(err)
	}
	throttle, err = buildThrottle(buildCtx, cfg, log)
	if err != nil {
		return fail(err)
	}
	accounts, err := buildAccounts(buildCtx, accountsBuildInput{
		cfg:      cfg,
		store:    st,
		identity: ids,
		throttle: throttle,
		log:      log,
	})
	if err != nil {
		return fail(err)
	}

	recorder, err := audit.New(st, log, audit.Options{
		RetentionDays:  cfg.Audit.RetentionDays,
		QueueSize:      cfg.Audit.QueueSize,
		FlushInterval:  cfg.Audit.FlushInterval,
		StatsInterval:  cfg.Audit.StatsInterval,
		File:           cfg.Audit.File,
		FileMaxSizeMB:  cfg.Audit.FileMaxSizeMB,
		FileMaxBackups: cfg.Audit.FileMaxBackups,
		FileMaxAgeDays: cfg.Audit.FileMaxAgeDays,
		FileCompress:   cfg.Audit.FileCompress,
	})
	if err != nil {
		return nil, fmt.Errorf("app: build audit: %w", err)
	}

	a := &App{
		cfg:      cfg,
		log:      log,
		ready:    httpx.NewReadiness(),
		store:    st,
		identity: ids,
		accounts: accounts,
		tenants:  tenant.New(st, accounts, accounts.InvalidatePrincipalCache),
		redis:    throttle.redis,
		audit:    recorder,
	}
	a.ready.Add("store", st.Ping)

	if cfg.Auth.ZeroTrust.Enabled {
		v, err := zerotrust.New(cfg.Auth.ZeroTrust.Secrets)
		if err != nil {
			return fail(fmt.Errorf("app: build zerotrust verifier: %w", err))
		}
		a.zerotrust = v
	}
	return a, nil
}

func buildIdentity(
	ctx context.Context,
	cfg config.Config,
	st *store.Store,
	log *slog.Logger,
) (*identity.Service, error) {
	static := make([]identity.StaticTenant, 0, len(cfg.Identity.TenantTokens))
	for _, t := range cfg.Identity.TenantTokens {
		static = append(static, identity.StaticTenant{Token: t.Token, UserID: t.UserID, CubeID: t.CubeID})
	}
	ids := identity.New(st, static, cfg.Identity.AdminToken, log)
	if err := ids.Bootstrap(ctx); err != nil {
		return nil, fmt.Errorf("app: bootstrap identity: %w", err)
	}
	return ids, nil
}

// throttleSetup 是登录失败节流的装配结果；未配置 Redis 时 redis 为 nil，只在进程内计数。
type throttleSetup struct {
	throttler platformthrottle.Throttler
	redis     *rediscache.Conn
}

// buildThrottle 装配登录失败节流。配置了 Redis URL 时先 PING：失败只告警，
// 每次调用仍会走 Redis 并在出错时回退到进程内存，不影响启动。
func buildThrottle(ctx context.Context, cfg config.Config, log *slog.Logger) (throttleSetup, error) {
	if cfg.Redis.URL == "" {
		// 不在这里另建内存节流：失败次数与窗口的默认值只在 account.New 里定义一次，
		// 这里传零值会把窗口设成 0，节流反而永久失效。
		return throttleSetup{}, nil
	}
	memory := platformthrottle.NewMemory(cfg.Auth.MaxLoginFailures, cfg.Auth.LoginFailWindow)
	conn, err := rediscache.Open(cfg.Redis.URL, cfg.Redis.Timeout)
	if err != nil {
		return throttleSetup{}, fmt.Errorf("app: open redis: %w", err)
	}
	pingCtx, cancel := context.WithTimeout(ctx, cfg.Redis.Timeout)
	defer cancel()
	if err := conn.Ping(pingCtx); err != nil {
		log.WarnContext(ctx, "redis unreachable; login throttle falls back to memory", slog.Any("err", err))
	}
	backend := rediscache.NewThrottle(conn, cfg.Auth.MaxLoginFailures, cfg.Auth.LoginFailWindow, cfg.Redis.Prefix)
	return throttleSetup{
		throttler: platformthrottle.NewFallback(backend, memory, log),
		redis:     conn,
	}, nil
}

func closeQuietly(conn *rediscache.Conn) {
	if conn != nil {
		_ = conn.Close()
	}
}

// accountsBuildInput 是账号服务的装配入参。
type accountsBuildInput struct {
	cfg      config.Config
	store    *store.Store
	identity *identity.Service
	throttle throttleSetup
	log      *slog.Logger
}

func buildAccounts(ctx context.Context, in accountsBuildInput) (*account.Service, error) {
	cfg := in.cfg
	accounts, err := account.New(in.store, in.identity, account.Options{
		SessionTTL:         cfg.Auth.SessionTTL,
		SessionMaxLifetime: cfg.Auth.SessionMaxLifetime,
		SeedUser:           cfg.Auth.SeedUser,
		SeedPassword:       cfg.Auth.SeedPassword,
		MaxFailures:        cfg.Auth.MaxLoginFailures,
		FailWindow:         cfg.Auth.LoginFailWindow,
		InviteDefaultTTL:   cfg.Auth.InviteDefaultTTL,
		InviteMaxTTL:       cfg.Auth.InviteMaxTTL,
		External: account.ExternalOptions{
			Enabled:      cfg.Auth.ZeroTrust.Enabled,
			Required:     cfg.Auth.ZeroTrust.Required,
			AutoCreate:   cfg.Auth.ZeroTrust.AutoCreate,
			AllowedUsers: cfg.Auth.ZeroTrust.AllowedUsers,
		},
	}, account.WithLogger(in.log), account.WithThrottler(in.throttle.throttler))
	if err != nil {
		return nil, fmt.Errorf("app: build accounts: %w", err)
	}
	if err := accounts.Bootstrap(ctx); err != nil {
		return nil, fmt.Errorf("app: bootstrap accounts: %w", err)
	}
	return accounts, nil
}

// buildWeb 在配置了静态目录时挂载前端产物；目录不合法直接构造失败。
func (a *App) buildWeb(cfg config.Config) error {
	if cfg.HTTP.WebDir == "" {
		return nil
	}
	web, err := httpx.SPA(cfg.HTTP.WebDir)
	if err != nil {
		return fmt.Errorf("app: build web: %w", err)
	}
	a.web = web
	return nil
}

// buildConsole 装配 console 角色的 Dashboard、前端与聊天路由。
func (a *App) buildConsole(cfg config.Config, client *upstream.Client, localSync dashboardTransport.LocalSync) error {
	if err := a.buildDashboard(cfg, a.identity, client, localSync); err != nil {
		return err
	}
	if err := a.buildWeb(cfg); err != nil {
		return err
	}
	if cfg.Chat.Dir != "" {
		a.buildChat(cfg)
	}
	return nil
}

func (a *App) buildDashboard(cfg config.Config, ids *identity.Service, client *upstream.Client, localSync dashboardTransport.LocalSync) error {
	deps := dashboardTransport.Deps{
		Tenants:   a.tenants,
		Tokens:    ids,
		Memory:    a.memory,
		Memories:  client,
		Templates: client,
		Prompts:   client,
		PromptStudio: prompt.New(upstream.NewPromptGateway(client), prompt.Options{
			TestBurst:            cfg.Prompt.TestBurst,
			TestRefillPerMin:     cfg.Prompt.TestRefillPerMin,
			GenerateBurst:        cfg.Prompt.GenerateBurst,
			GenerateRefillPerMin: cfg.Prompt.GenerateRefillPerMin,
		}),
		Models:        client,
		Accounts:      a.accounts,
		Knowledge:     client,
		Audit:         a.audit,
		Graph:         client,
		LocalSync:     localSync,
		Service:       cfg.ServiceName,
		GraphMaxNodes: cfg.Limits.GraphMaxNodes,
		WebDir:        cfg.HTTP.WebDir,
		AuthMode:      authMode(cfg),
		Logger:        a.log,
	}
	// 只有 all/mcp 角色才有同进程的 MCP 监听；console 单独部署时这里留空，
	// /api/health 会如实报网关不可达，而不是假装正常。
	if cfg.Role == config.RoleAll {
		deps.GatewayURL = localBaseURL(cfg.HTTP.MCPAddr)
	}
	dashboard, err := dashboardTransport.New(deps)
	if err != nil {
		return fmt.Errorf("app: build dashboard: %w", err)
	}
	a.dashboard = dashboard
	authServer, err := authTransport.New(authTransport.Deps{
		Onboarding:         onboarding.New(a.store),
		Sessions:           a.accounts,
		Tenants:            a.tenants,
		Invites:            a.accounts,
		Audit:              a.audit,
		SessionTTL:         cfg.Auth.SessionTTL,
		SessionMaxLifetime: cfg.Auth.SessionMaxLifetime,
		LoginFailWindow:    cfg.Auth.LoginFailWindow,
		CookieSecure:       cfg.Auth.CookieSecure,
		DisablePassword:    !cfg.Auth.AllowPassword,
		LogoutURL:          cfg.Auth.ZeroTrust.LogoutURL,
		Logger:             a.log,
	})
	if err != nil {
		return fmt.Errorf("app: build auth: %w", err)
	}
	a.auth = authServer
	return nil
}

// buildChat 装配 pi 运行时与聊天路由。启用条件只有 MEMOSD_CHAT_DIR 非空；
// 构造失败只告警并关闭聊天，不让整个控制面起不来（配置项已由 config 校验）。
func (a *App) buildChat(cfg config.Config) {
	runtime, err := piagent.New(piagent.Options{
		Dir:                cfg.Chat.Dir,
		PiBin:              cfg.Chat.PiBin,
		MCPURL:             cfg.Chat.MCPURL,
		ContextWindow:      cfg.Chat.ContextWindow,
		MaxTokens:          cfg.Chat.MaxTokens,
		MaxProcs:           cfg.Chat.MaxProcs,
		IdleTTL:            cfg.Chat.IdleTTL,
		SystemPrompt:       cfg.Chat.SystemPrompt,
		Tools:              cfg.Chat.Tools,
		ProcMemoryBudget:   cfg.Chat.ProcMemory,
		MinFreeDisk:        cfg.Chat.MinFreeDisk,
		Metrics:            a.metrics,
		ConversationTitles: a.store,
	}, a.log)
	if err != nil {
		a.log.Warn("chat disabled", slog.Any("err", err))
		return
	}
	// 上一代进程可能因 Pod 重启遗留在同一容器里；先定向回收再开始接流量。
	runtime.SweepOrphans()

	svc, err := chatservice.New(runtime, a.store, a.tenants, a.identity, chatservice.Options{
		Conversations: a.store,
		Feedback:      a.store,
		ContextWindow: cfg.Chat.ContextWindow,
		MaxTokens:     cfg.Chat.MaxTokens,
		Logger:        a.log,
		DefaultConfig: domain.ChatConfig{
			BaseURL: cfg.Chat.BaseURL,
			Model:   cfg.Chat.Model,
			APIKey:  cfg.Chat.APIKey,
		},
	})
	if err != nil {
		a.log.Warn("chat disabled", slog.Any("err", err))
		_ = runtime.Close()
		return
	}
	tr, err := chatTransport.New(chatTransport.Deps{
		Chat:     svc,
		Accounts: a.accounts,
		Tokens:   a.identity,
		Audit:    a.audit,
		Logger:   a.log,
	})
	if err != nil {
		a.log.Warn("chat disabled", slog.Any("err", err))
		_ = runtime.Close()
		return
	}
	a.piagent = runtime
	a.chat = tr
	a.log.Info("chat enabled", slog.String("dir", cfg.Chat.Dir))
}

// authMode 告诉前端当前的身份来源：零信任必选时本地会话不再参与鉴权。
func authMode(cfg config.Config) string {
	if cfg.Auth.ZeroTrust.Required {
		return dashboardTransport.AuthModeExternal
	}
	return dashboardTransport.AuthModePage
}

func serverOptions(cfg config.Config) httpx.ServerOptions {
	return httpx.ServerOptions{
		ReadHeaderTimeout: cfg.HTTP.ReadHeaderTimeout,
		ReadTimeout:       cfg.HTTP.ReadTimeout,
		ShutdownTimeout:   cfg.HTTP.ShutdownTimeout,
	}
}

// buildLocalSync 构造本地同步服务与入口，并决定 Dashboard 是否注入该能力。
// 能力关闭时入口仍挂载（统一回 503），但 Dashboard 不注入，避免读到旧数据。
func (a *App) buildLocalSync(cfg config.Config, log *slog.Logger) (*localsyncsvc.Service, dashboardTransport.LocalSync, error) {
	svc := localsyncsvc.New(a.store, a.memory, a.audit, localsyncOptions(cfg), log)
	if cfg.Role != config.RoleConsole && cfg.Role != config.RoleAll {
		return svc, nil, nil
	}
	srv, err := localsyncTransport.New(localsyncTransport.Deps{
		Service:         svc,
		Accounts:        a.accounts,
		Tenants:         a.store,
		Enabled:         cfg.LocalSync.Enabled,
		DisablePassword: !cfg.Auth.AllowPassword,
		Logger:          log,
	})
	if err != nil {
		return nil, nil, fmt.Errorf("app: build local sync: %w", err)
	}
	a.localSync = srv
	if !cfg.LocalSync.Enabled {
		return svc, nil, nil
	}
	return svc, svc, nil
}

// localsyncOptions 把配置值映射成 service 选项；零值由 service 自己补默认。
func localsyncOptions(cfg config.Config) localsyncsvc.Options {
	ls := cfg.LocalSync
	return localsyncsvc.Options{
		DeviceCodeTTL:        ls.DeviceCodeTTL,
		PollInterval:         ls.PollInterval,
		TokenTTL:             ls.TokenTTL,
		BatchTTL:             ls.BatchTTL,
		MaxChunkBytes:        ls.MaxChunkBytes,
		MaxBatchBytes:        ls.MaxBatchBytes,
		MaxBatchDecompressed: ls.MaxBatchDecompressed,
		MaxRowBytes:          ls.MaxRowBytes,
		QuotaBytes:           ls.QuotaBytes,
		QuotaRows:            ls.QuotaRows,
		RatePerMinute:        ls.RatePerMinute,
		VerificationURI:      ls.VerificationURI,
		DeletedRetention:     ls.DeletedRetention,
		MirrorRetention:      ls.MirrorRetention,
		TombstoneRetention:   ls.TombstoneRetention,
	}
}

// localBaseURL 把监听地址（可能是 :8002 或 0.0.0.0:8002）变成可拨号的集群内 URL。
// 健康探测必须落到本机，不能填 0.0.0.0；绑定通配地址时统一改用回环。
func localBaseURL(addr string) string {
	host, port, err := net.SplitHostPort(addr)
	if err != nil || port == "" {
		return ""
	}
	switch host {
	case "", "0.0.0.0", "::", "[::]":
		host = "127.0.0.1"
	}
	return "http://" + net.JoinHostPort(host, port)
}

// middleware 是所有角色共用的入站基础件。role 只用于指标标签。
func (a *App) middleware(role string, next http.Handler, extra ...func(http.Handler) http.Handler) http.Handler {
	chain := []func(http.Handler) http.Handler{
		httpx.RequestID,
		httpx.ClientIP,
		httpx.Recover(a.log),
		httpx.AccessLog(a.log, httpx.AccessLogOptions{Role: role, Metrics: a.metrics}),
		httpx.CORS(a.cfg.HTTP.CORSOrigins),
		httpx.MaxBody(a.cfg.HTTP.MaxBodyBytes),
	}
	return httpx.Chain(next, append(chain, extra...)...)
}

// consoleHandler 挂登录入口、Dashboard 与 Admin 路由。
func (a *App) consoleHandler() http.Handler {
	mux := http.NewServeMux()
	a.mountHealth(mux)
	if a.admin != nil {
		mux.Handle("/admin/", a.admin.Handler())
	}
	if a.auth != nil {
		login := a.auth.Handler()
		mux.Handle("/__login", login)
		mux.Handle("/__logout", login)
		mux.Handle("/__relogin", login)
		mux.Handle("/__invite/", login)
		// 账号自服务（me/password/sessions）都挂在 /api/auth/ 下。
		mux.Handle("/api/auth/", login)
	}
	if a.chat != nil {
		// /api/chat/ 比 /api/ 更具体，ServeMux 会优先匹配。
		mux.Handle("/api/chat/", a.chat.Handler())
	}
	if a.dashboard != nil {
		mux.Handle("/api/", a.dashboard.Handler())
	}
	if a.localSync != nil {
		// 比 /api/ 更具体，ServeMux 会优先匹配；/__device 是浏览器授权页。
		mux.Handle("/api/local-sync/", a.localSync.Handler())
		mux.Handle("/__device", a.localSync.Handler())
	}
	if a.web != nil {
		mux.Handle("/", a.web)
	} else {
		// 未配置前端产物时，非 API 的未知路径也回 JSON 信封，
		// 不再落到 Go 默认的 text/plain 404。
		mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
			httpx.NotFoundJSON(w)
		})
	}
	if a.zerotrust == nil {
		return a.middleware("console", mux)
	}
	// 顺序有意义：LoggedOutGate 必须包在 ZeroTrust 外面，否则退出后的请求会先被
	// 网关身份重新登录，标记形同虚设。
	return a.middleware("console", mux, httpx.LoggedOutGate, httpx.ZeroTrust(a.zerotrust, zerotrust.HeaderName))
}

// mcpHandler 挂 MCP 与 REST 路由。
func (a *App) mcpHandler() http.Handler {
	mux := http.NewServeMux()
	a.mountHealth(mux)
	if a.mcp != nil {
		mux.Handle("/mcp", a.mcp.Handler())
	}
	if a.rest != nil {
		mux.Handle("/memory/", a.rest.Handler())
	}
	if a.plugin != nil {
		mux.Handle("/memory/plugin/", a.plugin.Handler())
	}
	// 未知路径也回 JSON 信封；/healthz、/readyz 是更具体的 pattern，不受影响。
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		httpx.NotFoundJSON(w)
	})
	return a.middleware("mcp", mux)
}

// pprofHandler 只挂在独立监听端口上，默认不启用；不要通过 Service 暴露。
func pprofHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/debug/pprof/", pprof.Index)
	mux.HandleFunc("/debug/pprof/cmdline", pprof.Cmdline)
	mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
	mux.HandleFunc("/debug/pprof/symbol", pprof.Symbol)
	mux.HandleFunc("/debug/pprof/trace", pprof.Trace)
	return mux
}

// metricsHandler 只暴露 /metrics；独立端口，不走业务鉴权，也不记录访问日志。
func metricsHandler(m *metrics.Metrics) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", m.Handler())
	return mux
}

// mountHealth 挂健康检查。
func (a *App) mountHealth(mux *http.ServeMux) {
	mux.Handle("GET /healthz", httpx.HealthHandler())
	mux.Handle("GET /readyz", a.ready.Handler())
}

// Handlers 返回按角色构造的 handler，供测试在不起监听的情况下验证路由。
func (a *App) Handlers() map[string]http.Handler {
	out := make(map[string]http.Handler, len(a.servers))
	for _, s := range a.servers {
		out[s.name] = s.srv.Handler
	}
	return out
}

// Run 启动全部 server，直到 ctx 取消或某个 server 失败，然后优雅停机。
func (a *App) Run(parent context.Context) error {
	ctx, cancel := context.WithCancelCause(parent)
	defer cancel(nil)

	// worker 用独立生命周期：先停入站，再停止认领并等待在途任务记账结束。
	workerDone, workerCancel := a.startWorker(parent)
	defer workerCancel()

	// 指标采样与业务生命周期一致；停机时先停采样，避免读已关闭的库。
	stopMetrics := a.startMetricsSampler(ctx)
	defer stopMetrics()

	auditDone := make(chan error, 1)
	go func() { auditDone <- a.audit.Run(ctx) }()

	errCh := make(chan error, len(a.servers))
	var wg sync.WaitGroup
	for _, s := range a.servers {
		wg.Add(1)
		go func(s namedServer) {
			defer wg.Done()
			a.log.InfoContext(ctx, "server starting",
				slog.String("role", s.name),
				slog.String("addr", s.srv.Addr),
			)
			if err := s.srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				wrapped := fmt.Errorf("serve %s on %s: %w", s.name, s.srv.Addr, err)
				errCh <- wrapped
				cancel(wrapped)
			}
		}(s)
	}

	var runErr error
	select {
	case <-ctx.Done():
		// 父 ctx 取消是正常停机；server 失败会通过 CancelCause 带出原因，
		// 避免 errCh 与 ctx.Done() 同时就绪时吞掉致命错误。
		runErr = context.Cause(ctx)
		if errors.Is(runErr, context.Canceled) || errors.Is(runErr, context.DeadlineExceeded) {
			runErr = nil
			a.log.InfoContext(ctx, "shutting down", slog.String("reason", "context"))
		} else {
			a.log.ErrorContext(ctx, "server failed", slog.String("error", runErr.Error()))
		}
	case err := <-errCh:
		runErr = err
		a.log.ErrorContext(ctx, "server failed", slog.String("error", err.Error()))
	}

	// SSE handler 只在订阅 channel 关闭或请求取消时返回，而 http.Server.Shutdown
	// 不会取消在途请求。先停 chat 进程让订阅关闭，否则每次停服都要等满
	// ShutdownTimeout 并报 shutdown incomplete。
	if a.piagent != nil {
		//nolint:contextcheck // 停机清理不能用已被取消的请求 ctx，内部按进程超时处理
		if err := a.piagent.Close(); err != nil {
			a.log.WarnContext(ctx, "close chat runtime", slog.String("error", err.Error()))
		}
	}

	if err := a.shutdown(ctx); err != nil {
		a.log.WarnContext(ctx, "shutdown incomplete", slog.String("error", err.Error()))
	}

	// 入站已停，等待审计队列排空；finish 之后 Close 才能安全关库。
	select {
	case err := <-auditDone:
		if err != nil {
			a.log.WarnContext(ctx, "audit stopped with error", slog.String("error", err.Error()))
		}
	case <-time.After(a.cfg.HTTP.ShutdownTimeout):
		a.log.WarnContext(ctx, "audit did not drain in time")
	}

	a.stopWorker(ctx, workerDone, workerCancel)

	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(a.cfg.HTTP.ShutdownTimeout):
		a.log.WarnContext(ctx, "server goroutines did not exit in time")
	}
	return runErr
}

// metricsSampleInterval 是积压/队列类 gauge 的采样周期；计数器不依赖它。
const metricsSampleInterval = 15 * time.Second

// defaultMigrationTimeout 只在调用方直接构造 Config（未走 Load 默认值）时兜底；
// 正常配置来自 MEMOSD_MIGRATION_TIMEOUT。
const defaultMigrationTimeout = 10 * time.Minute

// assembleTimeout 是迁移之后其余装配步骤（身份 Bootstrap、节流后端、账号引导）
// 的预算。与迁移分开计时：大库迁移可能用满自己的预算，不能连累后续步骤。
const assembleTimeout = 30 * time.Second

// startMetricsSampler 周期性采样任务积压、审计队列与 SQLite 连接池。
// 返回停止函数，调用后等待采样 goroutine 退出。
func (a *App) startMetricsSampler(ctx context.Context) func() {
	if a.metrics == nil || a.store == nil {
		return func() {}
	}
	loopCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	sampler := &metricsSampler{metrics: a.metrics, store: a.store, audit: a.audit}
	go func() {
		defer close(done)
		sampler.sample(loopCtx)
		ticker := time.NewTicker(metricsSampleInterval)
		defer ticker.Stop()
		for {
			select {
			case <-loopCtx.Done():
				return
			case <-ticker.C:
				sampler.sample(loopCtx)
			}
		}
	}()
	return func() {
		cancel()
		<-done
	}
}

// metricsSampler 持有两次采样之间的累计值，用来把单调计数器换算成增量。
type metricsSampler struct {
	metrics *metrics.Metrics
	store   *store.Store
	audit   *audit.Recorder

	lastFull, lastBudget int64
	lastWritten          int64
	lastAuditFailed      int64
	lastFileFailed       int64
	lastWaitCount        int64
	lastWaitDuration     time.Duration
}

func (s *metricsSampler) sample(ctx context.Context) {
	s.sampleJobs(ctx)
	s.sampleAudit()
	s.sampleDB()
}

func (s *metricsSampler) sampleJobs(ctx context.Context) {
	pending, running, failed, oldest, err := s.store.JobStats(ctx)
	if err != nil {
		return
	}
	var age time.Duration
	if !oldest.IsZero() {
		age = time.Since(oldest)
	}
	s.metrics.SetJobs(pending, running, failed, age)
}

func (s *metricsSampler) sampleAudit() {
	if s.audit == nil {
		return
	}
	depth, capacity, bytes, droppedFull, droppedBudget := s.audit.Snapshot()
	s.metrics.SetAuditQueue(depth, capacity, bytes)
	if droppedFull >= s.lastFull {
		s.metrics.AddAuditDropped("queue_full", droppedFull-s.lastFull)
	}
	if droppedBudget >= s.lastBudget {
		s.metrics.AddAuditDropped("byte_budget", droppedBudget-s.lastBudget)
	}
	s.lastFull, s.lastBudget = droppedFull, droppedBudget

	written, failed, lastWrite := s.audit.Counters()
	var writtenDelta, failedDelta int64
	if written >= s.lastWritten {
		writtenDelta = written - s.lastWritten
	}
	if failed >= s.lastAuditFailed {
		failedDelta = failed - s.lastAuditFailed
	}
	s.metrics.SetAuditProgress(writtenDelta, failedDelta, lastWrite)
	s.lastWritten, s.lastAuditFailed = written, failed

	fileFailed := s.audit.FileFailures()
	if fileFailed >= s.lastFileFailed {
		s.metrics.AddAuditFileFailed(fileFailed - s.lastFileFailed)
	}
	s.lastFileFailed = fileFailed
}

func (s *metricsSampler) sampleDB() {
	write, read := s.store.DBStats()
	s.metrics.SetDBConns(write.InUse+read.InUse, write.Idle+read.Idle)
	waitCount := write.WaitCount + read.WaitCount
	waitDuration := write.WaitDuration + read.WaitDuration
	if waitCount >= s.lastWaitCount && waitDuration >= s.lastWaitDuration {
		s.metrics.AddDBWait(waitCount-s.lastWaitCount, waitDuration-s.lastWaitDuration)
	}
	s.lastWaitCount, s.lastWaitDuration = waitCount, waitDuration
}

// startWorker 启动任务轮询与保留清理；AddAsync 关闭时仍要跑清理。
func (a *App) startWorker(parent context.Context) (chan error, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.WithoutCancel(parent))
	done := make(chan error, 1)
	go func() { done <- a.worker.Run(ctx) }()
	return done, cancel
}

func (a *App) stopWorker(ctx context.Context, done chan error, cancel context.CancelFunc) {
	cancel()
	if done == nil {
		return
	}
	select {
	case err := <-done:
		if err != nil {
			a.log.WarnContext(ctx, "worker stopped with error", slog.String("error", err.Error()))
		}
	case <-time.After(a.cfg.HTTP.ShutdownTimeout + a.worker.DrainTimeout()):
		a.log.WarnContext(ctx, "worker did not stop in time")
	}
}

// shutdown 在超时内并发关闭全部 server。parent 只用于继承取消信号；
// 已经取消的 ctx 不能直接拿来做 Shutdown 的超时基准，因此先剥离取消。
func (a *App) shutdown(parent context.Context) error {
	timeout := a.cfg.HTTP.ShutdownTimeout
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), timeout)
	defer cancel()

	var wg sync.WaitGroup
	errs := make([]error, len(a.servers))
	for i, s := range a.servers {
		wg.Add(1)
		go func(i int, s namedServer) {
			defer wg.Done()
			if err := s.srv.Shutdown(ctx); err != nil {
				errs[i] = fmt.Errorf("shutdown %s: %w", s.name, err)
			}
		}(i, s)
	}
	wg.Wait()

	a.log.InfoContext(ctx, "servers stopped")
	return errors.Join(errs...)
}

// Close 释放资源。调用前 Run 应已返回（审计已排空）。
func (a *App) Close() error {
	var errs []error
	if a.audit != nil {
		errs = append(errs, a.audit.Close())
	}
	if a.piagent != nil {
		errs = append(errs, a.piagent.Close())
	}
	if a.redis != nil {
		errs = append(errs, a.redis.Close())
	}
	if a.store != nil {
		errs = append(errs, a.store.Close())
	}
	return errors.Join(errs...)
}

// withRuntimeConfig 只公开部署地址，前端模块加载前即可读取，不携带身份或凭据。
func withRuntimeConfig(next http.Handler, serviceURL, consoleURL string) http.Handler {
	config := httpx.PublicRuntimeConfig(serviceURL, consoleURL)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/runtime-config.js" {
			config.ServeHTTP(w, r)
			return
		}
		next.ServeHTTP(w, r)
	})
}
