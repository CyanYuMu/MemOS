package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Role 是进程的部署角色。同一个二进制按角色挂载不同的路由。
type Role string

const (
	RoleConsole Role = "console"
	RoleMCP     Role = "mcp"
	RoleAll     Role = "all"
)

// ParseRole 解析命令行或环境变量里的角色名。
func ParseRole(s string) (Role, error) {
	switch Role(strings.ToLower(strings.TrimSpace(s))) {
	case RoleConsole:
		return RoleConsole, nil
	case RoleMCP:
		return RoleMCP, nil
	case RoleAll:
		return RoleAll, nil
	default:
		return "", fmt.Errorf("unknown role %q (want console, mcp or all)", s)
	}
}

// TenantToken 是 MEMOS_TENANTS 解析后的一项。Token 明文只在启动期存在，
// 由 identity 转成 domain.Token 的哈希后不再保留。
type TenantToken struct {
	Token  string
	UserID string
	CubeID string
}

// Config 是进程全部配置。构造后不可变，其他包不得再读环境变量。
type Config struct {
	ServiceName string
	Role        Role
	Log         LogConfig
	HTTP        HTTPConfig
	Upstream    UpstreamConfig
	Skill       SkillConfig
	Store       StoreConfig
	Redis       RedisConfig
	Identity    IdentityConfig
	Auth        AuthConfig
	Limits      LimitsConfig
	Prompt      PromptConfig
	Worker      WorkerConfig
	Audit       AuditConfig
	Chat        ChatConfig
	LocalSync   LocalSyncConfig
}

// LogConfig 是日志器的构造参数。
type LogConfig struct {
	Level      string // debug / info / warn / error
	Format     string // json / text
	File       string // 非空时额外落盘并按大小轮转
	MaxSizeMB  int
	MaxBackups int
	MaxAgeDays int
	Compress   bool
}

// HTTPConfig 是入站服务与请求处理的上限。
type HTTPConfig struct {
	ConsoleAddr       string
	MCPAddr           string
	PprofAddr         string // 非空时在独立端口暴露 /debug/pprof；默认关闭
	MetricsAddr       string // 非空时在独立端口暴露 /metrics；默认关闭
	ReadHeaderTimeout time.Duration
	ReadTimeout       time.Duration
	ShutdownTimeout   time.Duration
	MaxBodyBytes      int64
	CORSOrigins       []string
	// WebDir 是控制台前端静态产物的目录；空表示不托管前端（只提供 API）。
	WebDir string
	// PluginDir 是 hermes 插件发布包目录（镜像里是 /app/plugins）；空表示不提供在线安装。
	PluginDir        string
	PublicServiceURL string
	PublicConsoleURL string
}

// UpstreamConfig 是数据面 memos-api 的连接参数。
type UpstreamConfig struct {
	BaseURL          string
	Timeout          time.Duration
	MaxResponseBytes int64
	// DashboardMaxBytes 是控制台记忆列表流式读取的原始字节上限。
	// 上游一页偏好记忆实测可达 148 MB，所以它与通用响应上限分开配置。
	DashboardMaxBytes int64
}

// SkillConfig 是可选的独立 Skill 管理服务；Token 只在服务器之间使用。
type SkillConfig struct {
	BaseURL string
	Token   string
}

// StoreConfig 是控制面自有数据的存储位置。
type StoreConfig struct {
	Path string
	// MigrationTimeout 是启动迁移的独立超时。迁移会重写全表时间列、建索引，
	// 大库可能超过 app 装配的 30s 超时；超时后事务回滚、进程重启会重复失败，
	// 因此迁移必须有自己更长、可配置的预算。
	MigrationTimeout time.Duration
}

// RedisConfig 是可选的共享状态配置；URL 为空时只用进程内存。
type RedisConfig struct {
	URL     string        // redis://[:password@]host:port/db；空表示不启用
	Prefix  string        // 节流键前缀，与同一 Redis 上的其它业务隔离
	Timeout time.Duration // 拨号与读写超时
}

// IdentityConfig 是启动期的静态身份来源。
type IdentityConfig struct {
	TenantTokens []TenantToken
	AdminToken   string
}

// AuthConfig 是控制台账号会话、登录节流与种子管理员的参数。
type AuthConfig struct {
	SeedUser           string
	SeedPassword       string // 空表示不种子
	SessionTTL         time.Duration
	SessionMaxLifetime time.Duration // 会话绝对上限，必须 >= SessionTTL
	CookieSecure       bool
	AllowPassword      bool          // false 时关闭 /__login，只允许零信任网关登录
	MaxLoginFailures   int           // 同一用户名或来源 IP 在窗口内允许的失败次数
	LoginFailWindow    time.Duration // 失败计数的滑动窗口
	InviteDefaultTTL   time.Duration // 创建邀请未指定过期时间时的有效期，默认 7 天
	InviteMaxTTL       time.Duration // 邀请允许的最长有效期，默认 30 天
	ZeroTrust          ZeroTrustConfig
}

// ZeroTrustConfig 是零信任 Web 网关注入身份的校验参数。
type ZeroTrustConfig struct {
	Enabled      bool
	Required     bool     // true：只允许外部身份或管理 Token，本地会话不再单独通过
	AutoCreate   bool     // true：首次出现的 username 自动建无口令账号
	Secrets      []string // 网关平台的 JWT Secret；轮换期可同时配置新旧两个，任一匹配即通过
	AllowedUsers []string // 逗号分隔的 username 白名单；空表示不限制
	LogoutURL    string   // 外部身份登出后的跳转地址；空表示只清本地会话
}

// LimitsConfig 是对外接口的取值上限。
type LimitsConfig struct {
	DefaultTopK   int
	MaxTopK       int
	DreamTopK     int    // 0 表示不召回 dream 桶
	GraphMaxNodes int    // 控制台图谱单次返回的节点上限
	QueryPrefix   string // 叠加在 search query 前，空表示不加
}

// PromptConfig 是提示词工作台的进程内限流参数。
// 生产是单副本，令牌桶只在内存里；多副本时限额会按副本数放大（见 prompt-studio.md）。
type PromptConfig struct {
	TestBurst            int
	TestRefillPerMin     int
	GenerateBurst        int
	GenerateRefillPerMin int
}

// WorkerConfig 是异步写入的轮询、重试与执行上限。
type WorkerConfig struct {
	AddAsync       bool
	PollInterval   time.Duration
	JobTimeout     time.Duration // 单个任务执行上限
	MaxAttempts    int           // 单个任务的最大尝试次数
	RetryBase      time.Duration // 首次重试等待，之后按 2 的幂递增
	RetryMax       time.Duration // 单次重试等待上限
	Concurrency    int           // 同时消费的任务数；同一租户始终串行
	RetentionDays  int           // 终态任务的保留天数，超期由 worker 清理
	IdempotencyTTL time.Duration // 写入幂等记录的保留时长
}

// AuditConfig 是审计记录的保留、缓冲与归档策略。
type AuditConfig struct {
	RetentionDays int
	QueueSize     int
	FlushInterval time.Duration
	StatsInterval time.Duration
	// File 非空时追加完整 JSONL 归档；与 SQLite 查询副本分开。
	File           string
	FileMaxSizeMB  int
	FileMaxBackups int // 0 表示保留全部轮转文件
	FileMaxAgeDays int // 0 表示不按天数删除
	FileCompress   bool
}

// ChatConfig 是管理后台 Chat 页面的 pi 运行时参数。
// Dir 为空表示本进程不启用聊天接口；其余字段只在启用时生效。
type ChatConfig struct {
	Dir           string
	PiBin         string
	MaxProcs      int
	IdleTTL       time.Duration
	MCPURL        string
	ContextWindow int
	MaxTokens     int
	// SystemPrompt 为空时由 piagent 使用内置的记忆助手提示。
	SystemPrompt string
	// Tools 是直接暴露给模型的 MCP 工具；空时用 piagent 的默认集（检索/画像/记一下）。
	Tools []string
	// ProcMemory 是准入前要求的内存余量（字节）；0 表示用 piagent 的默认值。
	ProcMemory int64
	// MinFreeDisk 是会话目录所在盘的准入阈值（字节）；0 表示用默认值。
	MinFreeDisk int64
	// 内置对话模型：项目没有自配 BYOK 时直接用这一组，用户打开聊天页即可对话。
	BaseURL string
	Model   string
	APIKey  string
}

// LocalSyncConfig 是本地插件记忆同步通道的参数。Enabled=false 时相关接口显式 503。
type LocalSyncConfig struct {
	Enabled         bool
	VerificationURI string
	DeviceCodeTTL   time.Duration
	PollInterval    time.Duration
	TokenTTL        time.Duration
	BatchTTL        time.Duration
	MaxChunkBytes   int64
	MaxBatchBytes   int64
	// MaxBatchDecompressed 是一次 commit 全部块解压后的总量上限，默认 256 MiB。
	MaxBatchDecompressed int64
	MaxRowBytes          int
	QuotaBytes           int64
	QuotaRows            int64
	RatePerMinute        int
	// DeletedRetention 是控制台软删除行的物理清理期限。软删除立即释放配额，
	// 但行仍在库里占磁盘；超期后真正 DELETE，避免“删除→再上传”无限撑大共享库。
	DeletedRetention time.Duration
	// MirrorRetention 是本地记忆镜像的保留期；0 表示永久保留（默认）。
	// 只影响控制面的副本，不影响本地插件与云端记忆。
	MirrorRetention time.Duration
	// TombstoneRetention 是删除墓碑的保留期，默认 180 天。墓碑挡住同内容重传，
	// 长期保留会拖慢每次上传判定；0 表示永不清理。
	TombstoneRetention time.Duration
}

const (
	defaultServiceName         = "MemoryService"
	defaultConsoleAddr         = ":5280"
	defaultMCPAddr             = ":8002"
	defaultReadHeaderTimeout   = 10 * time.Second
	defaultReadTimeout         = 60 * time.Second
	defaultShutdownTimeout     = 25 * time.Second
	defaultMaxBodyBytes        = 4 << 20
	defaultUpstreamBaseURL     = "http://127.0.0.1:8000"
	defaultUpstreamTimeout     = 300 * time.Second // 搜索 p99 实测可达 157s
	defaultUpstreamMaxBytes    = 8 << 20           // metadata.sources 曾达 132MB
	defaultDBPath              = "data/memosd.db"
	defaultDefaultTopK         = 10
	defaultDreamTopK           = 2
	defaultWorkerPoll          = 2 * time.Second
	defaultWorkerConcurrency   = 4
	defaultJobTimeout          = 5 * time.Minute
	defaultAuditRetentionDays  = 30
	defaultDashboardMaxBytes   = 512 << 20
	defaultLoginFailures       = 5
	defaultLoginFailWindow     = 10 * time.Minute
	defaultInviteTTL           = 168 * time.Hour
	defaultInviteMaxTTL        = 720 * time.Hour
	defaultRedisPrefix         = "memosd:"
	defaultRedisTimeout        = 500 * time.Millisecond
	defaultMigrationTimeout    = 10 * time.Minute
	defaultGraphMaxNodes       = 4000
	defaultPromptTestBurst     = 3
	defaultPromptTestRefill    = 6
	defaultPromptGenBurst      = 2
	defaultPromptGenRefill     = 4
	maxGraphNodes              = 10000
	defaultJobMaxAttempts      = 3
	defaultJobRetryBase        = 5 * time.Second
	defaultJobRetryMax         = 10 * time.Minute
	defaultJobRetentionDays    = 7
	defaultIdempotencyTTL      = 24 * time.Hour
	defaultAuditQueueSize      = 1024
	defaultAuditFlushInterval  = time.Second
	defaultAuditStatsInterval  = 30 * time.Second
	defaultAuditFileMaxSizeMB  = 1024
	defaultAuditFileMaxBackups = 0
	defaultAuditFileMaxAgeDays = 0
	defaultLocalDeviceCodeTTL  = 10 * time.Minute
	defaultLocalPollInterval   = 5 * time.Second
	defaultLocalTokenTTL       = 180 * 24 * time.Hour
	defaultLocalBatchTTL       = 24 * time.Hour
	defaultLocalRatePerMinute  = 120
	defaultLogMaxSizeMB        = 1024
	defaultLogMaxBackups       = 5
	defaultLogMaxAgeDays       = 60
	defaultChatPiBin           = "pi"
	defaultChatMaxProcs        = 12
	defaultChatIdleTTL         = 15 * time.Minute
	defaultChatMCPURL          = "http://127.0.0.1:8002/mcp"
	defaultChatContextWindow   = 131072
	defaultChatMaxTokens       = 16384
	defaultAdminTokenMinLength = 16
	defaultSeedUser            = "admin"
	defaultSessionTTL          = 12 * time.Hour
	defaultSessionMaxLifetime  = 30 * 24 * time.Hour
	minZeroTrustSecretLength   = 16
	minSeedPasswordLength      = 8
)

// Load 读取环境变量并校验。整个进程只调用一次。
func Load() (Config, error) {
	return FromEnv(os.Getenv)
}

// FromEnv 用注入的 getenv 构造配置，便于测试。
func FromEnv(getenv func(string) string) (Config, error) {
	env := reader{getenv: getenv}

	role, err := ParseRole(env.str("MEMOSD_ROLE", string(RoleAll)))
	if err != nil {
		return Config{}, fmt.Errorf("MEMOSD_ROLE: %w", err)
	}

	// 解压上限是后加的：存量部署可能把单批上限调得比默认解压上限更大。未显式
	// 设置新变量时跟随单批上限，避免升级后直接启动失败。
	cfg := Config{
		ServiceName: env.str("MEMOSD_SERVICE_NAME", defaultServiceName),
		Role:        role,
		Log: LogConfig{
			Level:      strings.ToLower(env.str("MEMOSD_LOG_LEVEL", "info")),
			Format:     strings.ToLower(env.str("MEMOSD_LOG_FORMAT", "json")),
			File:       env.str("MEMOSD_LOG_FILE", ""),
			MaxSizeMB:  env.int("MEMOSD_LOG_MAX_MB", defaultLogMaxSizeMB),
			MaxBackups: env.int("MEMOSD_LOG_MAX_BACKUPS", defaultLogMaxBackups),
			MaxAgeDays: env.int("MEMOSD_LOG_MAX_AGE_DAYS", defaultLogMaxAgeDays),
			Compress:   env.boolean("MEMOSD_LOG_COMPRESS", true),
		},
		HTTP: httpConfigFromEnv(env),
		Upstream: UpstreamConfig{
			BaseURL:           strings.TrimRight(env.str("MEMOS_API_BASE_URL", defaultUpstreamBaseURL), "/"),
			Timeout:           env.duration("MEMOS_TIMEOUT", defaultUpstreamTimeout),
			MaxResponseBytes:  env.int64("MEMOSD_UPSTREAM_MAX_BYTES", defaultUpstreamMaxBytes),
			DashboardMaxBytes: env.int64("MEMOSD_UPSTREAM_DASHBOARD_MAX_BYTES", defaultDashboardMaxBytes),
		},
		Skill: skillConfigFromEnv(env),
		Store: StoreConfig{
			Path:             env.str("MEMOSD_DB_PATH", defaultDBPath),
			MigrationTimeout: env.duration("MEMOSD_MIGRATION_TIMEOUT", defaultMigrationTimeout),
		},
		Redis: RedisConfig{
			URL:     env.str("MEMOSD_REDIS_URL", ""),
			Prefix:  env.str("MEMOSD_REDIS_PREFIX", defaultRedisPrefix),
			Timeout: env.duration("MEMOSD_REDIS_TIMEOUT", defaultRedisTimeout),
		},
		Auth: AuthConfig{
			SeedUser:           strings.ToLower(strings.TrimSpace(env.str("MEMOSD_ADMIN_USER", defaultSeedUser))),
			SeedPassword:       env.str("MEMOSD_ADMIN_PASSWORD", ""),
			SessionTTL:         env.duration("MEMOSD_SESSION_TTL", defaultSessionTTL),
			SessionMaxLifetime: env.duration("MEMOSD_SESSION_MAX_LIFETIME", defaultSessionMaxLifetime),
			CookieSecure:       env.boolean("MEMOSD_COOKIE_SECURE", true),
			AllowPassword:      env.boolean("MEMOSD_AUTH_ALLOW_PASSWORD", true),
			MaxLoginFailures:   env.int("MEMOSD_LOGIN_MAX_FAILURES", defaultLoginFailures),
			LoginFailWindow:    env.duration("MEMOSD_LOGIN_FAIL_WINDOW", defaultLoginFailWindow),
			InviteDefaultTTL:   env.duration("MEMOSD_INVITE_DEFAULT_TTL", defaultInviteTTL),
			InviteMaxTTL:       env.duration("MEMOSD_INVITE_MAX_TTL", defaultInviteMaxTTL),
			ZeroTrust: ZeroTrustConfig{
				Enabled:      env.boolean("MEMOSD_ZEROTRUST_ENABLED", false),
				Required:     env.boolean("MEMOSD_ZEROTRUST_REQUIRED", false),
				AutoCreate:   env.boolean("MEMOSD_ZEROTRUST_AUTO_CREATE", true),
				Secrets:      env.list("MEMOSD_ZEROTRUST_SECRET"),
				AllowedUsers: env.list("MEMOSD_ZEROTRUST_ALLOWED_USERS"),
				LogoutURL:    env.str("MEMOSD_ZEROTRUST_LOGOUT_URL", ""),
			},
		},
		Limits: LimitsConfig{
			DefaultTopK:   env.int("MEMOSD_DEFAULT_TOP_K", defaultDefaultTopK),
			MaxTopK:       env.int("MEMOSD_MAX_TOP_K", domain.ContractTopKMax),
			DreamTopK:     env.int("MEMOSD_DREAM_TOP_K", defaultDreamTopK),
			GraphMaxNodes: env.int("MEMOSD_GRAPH_MAX_NODES", defaultGraphMaxNodes),
			QueryPrefix:   env.str("MEMORY_QUERY_PREFIX", ""),
		},
		Prompt: promptConfigFromEnv(env),
		Worker: WorkerConfig{
			AddAsync:       env.boolean("MEMOS_ADD_ASYNC", true),
			PollInterval:   env.duration("MEMOSD_WORKER_POLL", defaultWorkerPoll),
			JobTimeout:     env.duration("MEMOSD_JOB_TIMEOUT", defaultJobTimeout),
			MaxAttempts:    env.int("MEMOSD_JOB_MAX_ATTEMPTS", defaultJobMaxAttempts),
			RetryBase:      env.duration("MEMOSD_JOB_RETRY_BASE", defaultJobRetryBase),
			RetryMax:       env.duration("MEMOSD_JOB_RETRY_MAX", defaultJobRetryMax),
			Concurrency:    env.int("MEMOSD_WORKER_CONCURRENCY", defaultWorkerConcurrency),
			RetentionDays:  env.int("MEMOSD_JOB_RETENTION_DAYS", defaultJobRetentionDays),
			IdempotencyTTL: env.duration("MEMOSD_IDEMPOTENCY_TTL", defaultIdempotencyTTL),
		},
		Audit: AuditConfig{
			RetentionDays:  env.int("MEMOSD_AUDIT_RETENTION_DAYS", defaultAuditRetentionDays),
			QueueSize:      env.int("MEMOSD_AUDIT_QUEUE_SIZE", defaultAuditQueueSize),
			FlushInterval:  env.duration("MEMOSD_AUDIT_FLUSH_INTERVAL", defaultAuditFlushInterval),
			StatsInterval:  env.duration("MEMOSD_AUDIT_STATS_INTERVAL", defaultAuditStatsInterval),
			File:           env.str("MEMOSD_AUDIT_FILE", ""),
			FileMaxSizeMB:  env.int("MEMOSD_AUDIT_FILE_MAX_MB", defaultAuditFileMaxSizeMB),
			FileMaxBackups: env.int("MEMOSD_AUDIT_FILE_MAX_BACKUPS", defaultAuditFileMaxBackups),
			FileMaxAgeDays: env.int("MEMOSD_AUDIT_FILE_MAX_AGE_DAYS", defaultAuditFileMaxAgeDays),
			FileCompress:   env.boolean("MEMOSD_AUDIT_FILE_COMPRESS", true),
		},
		Chat: ChatConfig{
			Dir:           strings.TrimSpace(env.str("MEMOSD_CHAT_DIR", "")),
			PiBin:         env.str("MEMOSD_CHAT_PI_BIN", defaultChatPiBin),
			MaxProcs:      env.int("MEMOSD_CHAT_MAX_PROCS", defaultChatMaxProcs),
			IdleTTL:       env.duration("MEMOSD_CHAT_IDLE_TTL", defaultChatIdleTTL),
			MCPURL:        env.str("MEMOSD_CHAT_MCP_URL", defaultChatMCPURL),
			ContextWindow: env.int("MEMOSD_CHAT_CONTEXT_WINDOW", defaultChatContextWindow),
			MaxTokens:     env.int("MEMOSD_CHAT_MAX_TOKENS", defaultChatMaxTokens),
			ProcMemory:    env.int64("MEMOSD_CHAT_PROC_MEMORY", 0),
			MinFreeDisk:   env.int64("MEMOSD_CHAT_MIN_FREE_DISK", 0),
			SystemPrompt:  strings.TrimSpace(env.str("MEMOSD_CHAT_SYSTEM_PROMPT", "")),
			Tools:         env.list("MEMOSD_CHAT_TOOLS"),
			BaseURL:       strings.TrimSpace(env.str("MEMOSD_CHAT_BASE_URL", "")),
			Model:         strings.TrimSpace(env.str("MEMOSD_CHAT_MODEL", "")),
			APIKey:        strings.TrimSpace(env.str("MEMOSD_CHAT_API_KEY", "")),
		},
		LocalSync: localSyncConfig(env),
	}
	cfg.Identity.AdminToken = env.str("MEMOSD_ADMIN_TOKEN", "")

	tenants, err := parseTenants(env.str("MEMOS_TENANTS", ""))
	if err != nil {
		return Config{}, fmt.Errorf("MEMOS_TENANTS: %w", err)
	}
	cfg.Identity.TenantTokens = tenants

	if err := env.err(); err != nil {
		return Config{}, err
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// localSyncConfig 组装本地同步配置。默认关闭：插件端尚未发布，服务端先不暴露入口。
// 解压上限未显式配置时跟随单批上限，避免存量部署升级后直接启动失败。
func localSyncConfig(env reader) LocalSyncConfig {
	maxBatch := env.int64("MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES", domain.LocalBatchBytesLimit)
	maxDecompressed := env.int64("MEMOSD_LOCAL_SYNC_MAX_BATCH_DECOMPRESSED", domain.LocalBatchDecompressedLimit)
	if !env.has("MEMOSD_LOCAL_SYNC_MAX_BATCH_DECOMPRESSED") && maxDecompressed < maxBatch {
		maxDecompressed = maxBatch
	}
	return LocalSyncConfig{
		Enabled:              env.boolean("MEMOSD_LOCAL_SYNC_ENABLED", false),
		VerificationURI:      strings.TrimSpace(env.str("MEMOSD_LOCAL_SYNC_VERIFICATION_URI", "")),
		DeviceCodeTTL:        env.duration("MEMOSD_LOCAL_SYNC_DEVICE_CODE_TTL", defaultLocalDeviceCodeTTL),
		PollInterval:         env.duration("MEMOSD_LOCAL_SYNC_POLL_INTERVAL", defaultLocalPollInterval),
		TokenTTL:             env.duration("MEMOSD_LOCAL_SYNC_TOKEN_TTL", defaultLocalTokenTTL),
		BatchTTL:             env.duration("MEMOSD_LOCAL_SYNC_BATCH_TTL", defaultLocalBatchTTL),
		MaxChunkBytes:        env.int64("MEMOSD_LOCAL_SYNC_MAX_CHUNK_BYTES", domain.LocalChunkBytesLimit),
		MaxBatchBytes:        maxBatch,
		MaxBatchDecompressed: maxDecompressed,
		MaxRowBytes:          env.int("MEMOSD_LOCAL_SYNC_MAX_ROW_BYTES", domain.LocalRowPayloadLimit),
		QuotaBytes:           env.int64("MEMOSD_LOCAL_SYNC_QUOTA_BYTES", domain.LocalQuotaBytesLimit),
		QuotaRows:            env.int64("MEMOSD_LOCAL_SYNC_QUOTA_ROWS", domain.LocalRowCountLimit),
		RatePerMinute:        env.int("MEMOSD_LOCAL_SYNC_RATE_PER_MINUTE", defaultLocalRatePerMinute),
		DeletedRetention:     env.duration("MEMOSD_LOCAL_SYNC_DELETED_RETENTION", 7*24*time.Hour),
		MirrorRetention:      env.duration("MEMOSD_LOCAL_SYNC_MIRROR_RETENTION", 0),
		TombstoneRetention:   env.duration("MEMOSD_LOCAL_SYNC_TOMBSTONE_RETENTION", 180*24*time.Hour),
	}
}

// Validate 检查跨字段约束。错误信息只报变量名与约束，不回显取值。
func (c Config) Validate() error {
	for _, check := range []func() error{
		c.validateBasic, c.validateHTTP, c.validateUpstream, c.validateSkill,
		c.validateIdentity, c.validateAuth, c.validateLimits, c.validatePrompt, c.validateWorker, c.validateAudit,
		c.validateRedis, c.validateStore, c.validateChat, c.validateLocalSync,
	} {
		if err := check(); err != nil {
			return err
		}
	}
	return nil
}

func (c Config) validateBasic() error {
	if strings.TrimSpace(c.ServiceName) == "" {
		return errors.New("MEMOSD_SERVICE_NAME must not be empty")
	}
	switch c.Log.Level {
	case "debug", "info", "warn", "error":
	default:
		return errors.New("MEMOSD_LOG_LEVEL must be debug, info, warn or error")
	}
	switch c.Log.Format {
	case "json", "text":
	default:
		return errors.New("MEMOSD_LOG_FORMAT must be json or text")
	}
	if c.Log.MaxSizeMB < 1 {
		return errors.New("MEMOSD_LOG_MAX_MB must be at least 1")
	}
	if c.Log.MaxBackups < 0 {
		return errors.New("MEMOSD_LOG_MAX_BACKUPS must not be negative")
	}
	if c.Log.MaxAgeDays < 0 {
		return errors.New("MEMOSD_LOG_MAX_AGE_DAYS must not be negative")
	}
	return nil
}

func (c Config) validateHTTP() error {
	if c.HTTP.ConsoleAddr == "" {
		return errors.New("MEMOSD_CONSOLE_ADDR must not be empty")
	}
	if c.HTTP.MCPAddr == "" {
		return errors.New("MEMOSD_MCP_ADDR must not be empty")
	}
	if c.HTTP.PprofAddr == c.HTTP.ConsoleAddr || c.HTTP.PprofAddr == c.HTTP.MCPAddr {
		return errors.New("MEMOSD_PPROF_ADDR must differ from console and mcp addresses")
	}
	if addr := c.HTTP.MetricsAddr; addr != "" &&
		(addr == c.HTTP.ConsoleAddr || addr == c.HTTP.MCPAddr || addr == c.HTTP.PprofAddr) {
		return errors.New("MEMOSD_METRICS_ADDR must differ from console, mcp and pprof addresses")
	}
	if c.Role == RoleAll && c.HTTP.ConsoleAddr == c.HTTP.MCPAddr {
		return errors.New("MEMOSD_CONSOLE_ADDR and MEMOSD_MCP_ADDR must differ when role=all")
	}
	if c.HTTP.ReadHeaderTimeout <= 0 {
		return errors.New("MEMOSD_READ_HEADER_TIMEOUT must be positive")
	}
	if c.HTTP.ReadTimeout <= 0 {
		return errors.New("MEMOSD_READ_TIMEOUT must be positive")
	}
	if c.HTTP.ShutdownTimeout <= 0 {
		return errors.New("MEMOSD_SHUTDOWN_TIMEOUT must be positive")
	}
	if c.HTTP.MaxBodyBytes <= 0 {
		return errors.New("MEMOSD_MAX_BODY_BYTES must be positive")
	}
	if slices.Contains(c.HTTP.CORSOrigins, "*") {
		// 通配 CORS 会让任意网页读走带凭据的响应；BFF 曾因此泄露完整 token。
		return errors.New("MEMOSD_CORS_ORIGINS must not contain \"*\"")
	}
	return nil
}

func (c Config) validateUpstream() error {
	if err := validatePublicURLs(c.HTTP); err != nil {
		return err
	}
	if !strings.HasPrefix(c.Upstream.BaseURL, "http://") && !strings.HasPrefix(c.Upstream.BaseURL, "https://") {
		return errors.New("MEMOS_API_BASE_URL must start with http:// or https://")
	}
	if c.Upstream.Timeout < time.Second {
		return errors.New("MEMOS_TIMEOUT must be at least 1s")
	}
	if c.Upstream.MaxResponseBytes <= 0 {
		return errors.New("MEMOSD_UPSTREAM_MAX_BYTES must be positive")
	}
	if c.Upstream.DashboardMaxBytes <= 0 {
		return errors.New("MEMOSD_UPSTREAM_DASHBOARD_MAX_BYTES must be positive")
	}
	if strings.TrimSpace(c.Store.Path) == "" {
		return errors.New("MEMOSD_DB_PATH must not be empty")
	}
	return nil
}

func (c Config) validateIdentity() error {
	if c.Identity.AdminToken != "" && len(c.Identity.AdminToken) < defaultAdminTokenMinLength {
		return errors.New("MEMOSD_ADMIN_TOKEN must be at least 16 characters when set")
	}
	return nil
}

func (c Config) validateAuth() error {
	if c.Auth.SessionTTL < time.Minute {
		return errors.New("MEMOSD_SESSION_TTL must be at least 1m")
	}
	if c.Auth.SessionMaxLifetime < c.Auth.SessionTTL {
		return errors.New("MEMOSD_SESSION_MAX_LIFETIME must not be shorter than MEMOSD_SESSION_TTL")
	}
	if c.Auth.MaxLoginFailures < 1 {
		return errors.New("MEMOSD_LOGIN_MAX_FAILURES must be at least 1")
	}
	if c.Auth.LoginFailWindow < time.Second {
		return errors.New("MEMOSD_LOGIN_FAIL_WINDOW must be at least 1s")
	}
	if c.Auth.InviteDefaultTTL < time.Hour || c.Auth.InviteMaxTTL < time.Hour {
		return errors.New("MEMOSD_INVITE_DEFAULT_TTL and MEMOSD_INVITE_MAX_TTL must be at least 1h")
	}
	if c.Auth.InviteDefaultTTL > c.Auth.InviteMaxTTL {
		return errors.New("MEMOSD_INVITE_DEFAULT_TTL must not exceed MEMOSD_INVITE_MAX_TTL")
	}
	if err := c.validateZeroTrust(); err != nil {
		return err
	}
	if c.Auth.SeedPassword == "" {
		return nil
	}
	if len(c.Auth.SeedPassword) < minSeedPasswordLength {
		return errors.New("MEMOSD_ADMIN_PASSWORD must be at least 8 characters when set")
	}
	if c.Auth.SeedUser == "" || len(c.Auth.SeedUser) > 64 {
		return errors.New("MEMOSD_ADMIN_USER must be 1..64 characters when MEMOSD_ADMIN_PASSWORD is set")
	}
	return nil
}

func (c Config) validateZeroTrust() error {
	zt := c.Auth.ZeroTrust
	if zt.Required && !zt.Enabled {
		return errors.New("MEMOSD_ZEROTRUST_REQUIRED requires MEMOSD_ZEROTRUST_ENABLED=true")
	}
	if zt.Required && c.Auth.AllowPassword {
		return errors.New("MEMOSD_ZEROTRUST_REQUIRED=true requires MEMOSD_AUTH_ALLOW_PASSWORD=false")
	}
	if !zt.Enabled {
		return nil
	}
	if len(zt.Secrets) == 0 {
		return errors.New("MEMOSD_ZEROTRUST_SECRET must be set when enabled")
	}
	for _, secret := range zt.Secrets {
		if len(strings.TrimSpace(secret)) < minZeroTrustSecretLength {
			return fmt.Errorf("MEMOSD_ZEROTRUST_SECRET entries must be at least %d characters", minZeroTrustSecretLength)
		}
	}
	seen := make(map[string]struct{}, len(zt.AllowedUsers))
	for _, raw := range zt.AllowedUsers {
		name := strings.ToLower(strings.TrimSpace(raw))
		if name == "" {
			return errors.New("MEMOSD_ZEROTRUST_ALLOWED_USERS must not contain empty entries")
		}
		if _, ok := seen[name]; ok {
			return fmt.Errorf("MEMOSD_ZEROTRUST_ALLOWED_USERS contains duplicate %q", name)
		}
		seen[name] = struct{}{}
	}
	return nil
}

func (c Config) validateLimits() error {
	// 上游契约 v2 的 top_k 上限是 25（cloud recall cap），超过会让整个检索请求 422。
	if c.Limits.MaxTopK < 1 || c.Limits.MaxTopK > domain.ContractTopKMax {
		return fmt.Errorf("MEMOSD_MAX_TOP_K must be within 1..%d", domain.ContractTopKMax)
	}
	if c.Limits.DefaultTopK < 1 {
		return errors.New("MEMOSD_DEFAULT_TOP_K must be at least 1")
	}
	if c.Limits.DefaultTopK > c.Limits.MaxTopK {
		return errors.New("MEMOSD_DEFAULT_TOP_K must not exceed MEMOSD_MAX_TOP_K")
	}
	if c.Limits.DreamTopK < 0 {
		return errors.New("MEMOSD_DREAM_TOP_K must not be negative")
	}
	if c.Limits.GraphMaxNodes < 1 || c.Limits.GraphMaxNodes > maxGraphNodes {
		return errors.New("MEMOSD_GRAPH_MAX_NODES must be within 1..20000")
	}
	return nil
}

func (c Config) validatePrompt() error {
	for name, value := range map[string]int{
		"MEMOSD_PROMPT_TEST_BURST":              c.Prompt.TestBurst,
		"MEMOSD_PROMPT_TEST_REFILL_PER_MIN":     c.Prompt.TestRefillPerMin,
		"MEMOSD_PROMPT_GENERATE_BURST":          c.Prompt.GenerateBurst,
		"MEMOSD_PROMPT_GENERATE_REFILL_PER_MIN": c.Prompt.GenerateRefillPerMin,
	} {
		if value < 1 {
			return fmt.Errorf("%s must be at least 1", name)
		}
	}
	return nil
}

func (c Config) validateWorker() error {
	if c.Worker.PollInterval <= 0 {
		return errors.New("MEMOSD_WORKER_POLL must be positive")
	}
	if c.Worker.JobTimeout <= 0 {
		return errors.New("MEMOSD_JOB_TIMEOUT must be positive")
	}
	if c.Worker.Concurrency < 1 {
		return errors.New("MEMOSD_WORKER_CONCURRENCY must be at least 1")
	}
	if c.Worker.RetentionDays < 1 {
		return errors.New("MEMOSD_JOB_RETENTION_DAYS must be at least 1")
	}
	if c.Worker.IdempotencyTTL < time.Minute {
		return errors.New("MEMOSD_IDEMPOTENCY_TTL must be at least 1m")
	}
	if c.Worker.MaxAttempts < 1 {
		return errors.New("MEMOSD_JOB_MAX_ATTEMPTS must be at least 1")
	}
	if c.Worker.RetryBase <= 0 {
		return errors.New("MEMOSD_JOB_RETRY_BASE must be positive")
	}
	if c.Worker.RetryMax < c.Worker.RetryBase {
		return errors.New("MEMOSD_JOB_RETRY_MAX must not be smaller than MEMOSD_JOB_RETRY_BASE")
	}
	return nil
}

func (c Config) validateAudit() error {
	if c.Audit.RetentionDays < 1 {
		return errors.New("MEMOSD_AUDIT_RETENTION_DAYS must be at least 1")
	}
	if c.Audit.QueueSize < 1 {
		return errors.New("MEMOSD_AUDIT_QUEUE_SIZE must be at least 1")
	}
	if c.Audit.FlushInterval < 100*time.Millisecond {
		return errors.New("MEMOSD_AUDIT_FLUSH_INTERVAL must be at least 100ms")
	}
	if c.Audit.StatsInterval < time.Second {
		return errors.New("MEMOSD_AUDIT_STATS_INTERVAL must be at least 1s")
	}
	if c.Audit.FileMaxSizeMB < 0 {
		return errors.New("MEMOSD_AUDIT_FILE_MAX_MB must not be negative")
	}
	if c.Audit.FileMaxBackups < 0 {
		return errors.New("MEMOSD_AUDIT_FILE_MAX_BACKUPS must not be negative")
	}
	if c.Audit.FileMaxAgeDays < 0 {
		return errors.New("MEMOSD_AUDIT_FILE_MAX_AGE_DAYS must not be negative")
	}
	return nil
}

func (c Config) validateLocalSync() error {
	ls := c.LocalSync
	if ls.DeviceCodeTTL < time.Minute {
		return errors.New("MEMOSD_LOCAL_SYNC_DEVICE_CODE_TTL must be at least 1m")
	}
	if ls.PollInterval < time.Second {
		return errors.New("MEMOSD_LOCAL_SYNC_POLL_INTERVAL must be at least 1s")
	}
	if ls.TokenTTL < 24*time.Hour {
		return errors.New("MEMOSD_LOCAL_SYNC_TOKEN_TTL must be at least 24h")
	}
	if ls.BatchTTL < time.Hour {
		return errors.New("MEMOSD_LOCAL_SYNC_BATCH_TTL must be at least 1h")
	}
	if ls.MaxChunkBytes <= 0 || ls.MaxBatchBytes <= 0 || ls.MaxBatchDecompressed <= 0 || ls.QuotaBytes <= 0 {
		return errors.New("MEMOSD_LOCAL_SYNC_MAX_CHUNK_BYTES, MAX_BATCH_BYTES and QUOTA_BYTES must be positive")
	}
	if ls.MaxBatchDecompressed < ls.MaxBatchBytes {
		return errors.New("MEMOSD_LOCAL_SYNC_MAX_BATCH_DECOMPRESSED must be at least MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES")
	}
	if ls.MaxChunkBytes > ls.MaxBatchBytes {
		return errors.New("MEMOSD_LOCAL_SYNC_MAX_CHUNK_BYTES must not exceed MEMOSD_LOCAL_SYNC_MAX_BATCH_BYTES")
	}
	if c.Role != RoleMCP && ls.MaxChunkBytes > c.HTTP.MaxBodyBytes {
		// 分块正文经统一 MaxBody 中间件，块上限大于请求体上限时会在到达 handler 前被截断。
		return errors.New("MEMOSD_LOCAL_SYNC_MAX_CHUNK_BYTES must not exceed MEMOSD_MAX_BODY_BYTES")
	}
	if ls.MaxRowBytes <= 0 || ls.QuotaRows <= 0 || ls.RatePerMinute <= 0 {
		return errors.New("MEMOSD_LOCAL_SYNC_MAX_ROW_BYTES, QUOTA_ROWS and RATE_PER_MINUTE must be positive")
	}
	if ls.DeletedRetention < 0 {
		return errors.New("MEMOSD_LOCAL_SYNC_DELETED_RETENTION must not be negative")
	}
	if ls.MirrorRetention < 0 {
		return errors.New("MEMOSD_LOCAL_SYNC_MIRROR_RETENTION must not be negative")
	}
	if ls.TombstoneRetention < 0 {
		return errors.New("MEMOSD_LOCAL_SYNC_TOMBSTONE_RETENTION must not be negative")
	}
	if ls.VerificationURI != "" {
		u, err := url.Parse(ls.VerificationURI)
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
			return errors.New("MEMOSD_LOCAL_SYNC_VERIFICATION_URI must be an absolute http(s) URL")
		}
	}
	return nil
}

func (c Config) validateRedis() error {
	if c.Redis.URL != "" {
		u, err := url.Parse(c.Redis.URL)
		if err != nil {
			return errors.New("MEMOSD_REDIS_URL must be a valid URL")
		}
		if u.Scheme != "redis" && u.Scheme != "rediss" {
			return errors.New("MEMOSD_REDIS_URL must use redis:// or rediss://")
		}
		if u.Host == "" {
			return errors.New("MEMOSD_REDIS_URL must include a host")
		}
	}
	if strings.TrimSpace(c.Redis.Prefix) == "" {
		return errors.New("MEMOSD_REDIS_PREFIX must not be empty")
	}
	if c.Redis.Timeout <= 0 {
		return errors.New("MEMOSD_REDIS_TIMEOUT must be positive")
	}
	return nil
}

func (c Config) validateStore() error {
	if c.Store.MigrationTimeout <= 0 {
		return errors.New("MEMOSD_MIGRATION_TIMEOUT must be positive")
	}
	return nil
}

// validateChat 校验运行时参数。Dir 为空表示本进程不提供聊天接口，但其余字段
// 仍然检查：部署时把非法值暴露在启动期，比等到打开开关才发现要安全。
func (c Config) validateChat() error {
	if strings.TrimSpace(c.Chat.PiBin) == "" {
		return errors.New("MEMOSD_CHAT_PI_BIN must not be empty")
	}
	if c.Chat.MaxProcs < 1 {
		return errors.New("MEMOSD_CHAT_MAX_PROCS must be at least 1")
	}
	if c.Chat.IdleTTL <= 0 {
		return errors.New("MEMOSD_CHAT_IDLE_TTL must be positive")
	}
	u, err := url.Parse(c.Chat.MCPURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return errors.New("MEMOSD_CHAT_MCP_URL must be an http(s) URL with a host")
	}
	if c.Chat.ContextWindow < 1 {
		return errors.New("MEMOSD_CHAT_CONTEXT_WINDOW must be at least 1")
	}
	if c.Chat.MaxTokens < 1 {
		return errors.New("MEMOSD_CHAT_MAX_TOKENS must be at least 1")
	}
	if err := validateChatModel(c.Chat); err != nil {
		return err
	}
	if len(c.Chat.Tools) > 0 {
		for _, name := range c.Chat.Tools {
			if strings.TrimSpace(name) == "" {
				return errors.New("MEMOSD_CHAT_TOOLS must not contain empty entries")
			}
		}
	}
	return nil
}

// validateChatModel 要求内置模型三项成组出现：全空表示不提供默认模型，
// 用户必须自己配；只配一部分属于部署错误。
func validateChatModel(c ChatConfig) error {
	if c.BaseURL == "" && c.Model == "" && c.APIKey == "" {
		return nil
	}
	u, err := url.Parse(c.BaseURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return errors.New("MEMOSD_CHAT_BASE_URL must be an http(s) URL with a host")
	}
	if c.Model == "" {
		return errors.New("MEMOSD_CHAT_MODEL must not be empty when MEMOSD_CHAT_BASE_URL is set")
	}
	if c.APIKey == "" {
		return errors.New("MEMOSD_CHAT_API_KEY must not be empty when MEMOSD_CHAT_BASE_URL is set")
	}
	return nil
}

// Redacted 返回可安全写入日志的摘要：不含任何 token 明文或数量以外的身份信息。
func (c Config) Redacted() string {
	return fmt.Sprintf(
		"service=%s role=%s console=%s mcp=%s upstream=%s db=%s tenants=%d admin_token=%t seed_admin=%t zerotrust=%t allow_password=%t redis=%s chat=%t log=%s/%s",
		c.ServiceName, c.Role, c.HTTP.ConsoleAddr, c.HTTP.MCPAddr, redactURL(c.Upstream.BaseURL),
		c.Store.Path, len(c.Identity.TenantTokens), c.Identity.AdminToken != "",
		c.Auth.SeedPassword != "", c.Auth.ZeroTrust.Enabled, c.Auth.AllowPassword,
		redactURL(c.Redis.URL), c.Chat.Dir != "", c.Log.Level, c.Log.Format,
	)
}

// redactURL 去掉 URL 里的 userinfo，避免把上游凭据写进启动日志。
func redactURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.User == nil {
		return raw
	}
	u.User = nil
	return u.String()
}

// ApplyOverrides 应用命令行参数。校验仍由 Validate 统一负责。
func (c *Config) ApplyOverrides(role, consoleAddr, mcpAddr string) error {
	if role != "" {
		parsed, err := ParseRole(role)
		if err != nil {
			return err
		}
		c.Role = parsed
	}
	if consoleAddr != "" {
		c.HTTP.ConsoleAddr = consoleAddr
	}
	if mcpAddr != "" {
		c.HTTP.MCPAddr = mcpAddr
	}
	return nil
}

// curateAt 让部署方显式关闭后台扫描。环境变量为空会落回默认值，
// 所以关闭要用 off/none，而不是空串。
// reader 收集解析错误而不是在第一个错误处返回，方便一次暴露多个环境变量问题。
type reader struct {
	getenv func(string) string
	errs   []error
}

// has 报告变量是否显式设置（非空）。用于区分“用默认值”和“用户改了默认值”。
func (r *reader) has(key string) bool {
	return strings.TrimSpace(r.getenv(key)) != ""
}

func (r *reader) str(key, def string) string {
	if v := strings.TrimSpace(r.getenv(key)); v != "" {
		return v
	}
	return def
}

func (r *reader) list(key string) []string {
	raw := r.getenv(key)
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	out := make([]string, 0, 4)
	for part := range strings.SplitSeq(raw, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func (r *reader) duration(key string, def time.Duration) time.Duration {
	raw := strings.TrimSpace(r.getenv(key))
	if raw == "" {
		return def
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		r.errs = append(r.errs, fmt.Errorf("%s must be a duration (e.g. 30s): %w", key, err))
		return def
	}
	return d
}

func (r *reader) int(key string, def int) int {
	raw := strings.TrimSpace(r.getenv(key))
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		r.errs = append(r.errs, fmt.Errorf("%s must be an integer", key))
		return def
	}
	return n
}

func (r *reader) int64(key string, def int64) int64 {
	raw := strings.TrimSpace(r.getenv(key))
	if raw == "" {
		return def
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		r.errs = append(r.errs, fmt.Errorf("%s must be an integer number of bytes", key))
		return def
	}
	return n
}

func (r *reader) boolean(key string, def bool) bool {
	raw := strings.ToLower(strings.TrimSpace(r.getenv(key)))
	if raw == "" {
		return def
	}
	switch raw {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		r.errs = append(r.errs, fmt.Errorf("%s must be a boolean", key))
		return def
	}
}

func (r *reader) err() error {
	return errors.Join(r.errs...)
}

// parseTenants 支持与既有网关一致的两种写法：
// JSON {"tok":{"user_id":"u","cube_id":"c"}} 或紧凑 tok:u,tok2:u2。
func parseTenants(raw string) ([]TenantToken, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	if strings.HasPrefix(raw, "{") {
		return parseTenantsJSON(raw)
	}
	return parseTenantsCompact(raw)
}

func parseTenantsJSON(raw string) ([]TenantToken, error) {
	var spec map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &spec); err != nil {
		return nil, errors.New("malformed JSON")
	}
	out := make([]TenantToken, 0, len(spec))
	seen := make(map[string]struct{}, len(spec))
	for token, value := range spec {
		if token == "" {
			return nil, errors.New("tenant token must not be empty")
		}
		if _, dup := seen[token]; dup {
			return nil, errors.New("duplicate tenant token")
		}
		seen[token] = struct{}{}

		var userID string
		var entry struct {
			UserID string `json:"user_id"`
			CubeID string `json:"cube_id"`
		}
		if err := json.Unmarshal(value, &entry); err == nil {
			userID = strings.TrimSpace(entry.UserID)
			if userID == "" {
				return nil, fmt.Errorf("tenant entry %d has no user_id", len(out)+1)
			}
			cube := strings.TrimSpace(entry.CubeID)
			if cube == "" {
				cube = userID
			}
			out = append(out, TenantToken{Token: token, UserID: userID, CubeID: cube})
			continue
		}
		// 也接受 {"tok":"user_id"} 的简写。
		var plain string
		if err := json.Unmarshal(value, &plain); err != nil || strings.TrimSpace(plain) == "" {
			return nil, fmt.Errorf("tenant entry %d must be a string or {user_id, cube_id}", len(out)+1)
		}
		userID = strings.TrimSpace(plain)
		out = append(out, TenantToken{Token: token, UserID: userID, CubeID: userID})
	}
	return out, nil
}

func parseTenantsCompact(raw string) ([]TenantToken, error) {
	var out []TenantToken
	seen := make(map[string]struct{})
	for pair := range strings.SplitSeq(raw, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		// token 可能含冒号，按最后一个冒号切分；user_id 不允许含冒号。
		idx := strings.LastIndex(pair, ":")
		if idx <= 0 || idx == len(pair)-1 {
			return nil, fmt.Errorf("malformed tenant entry %d (want token:user_id)", len(out)+1)
		}
		token := strings.TrimSpace(pair[:idx])
		userID := strings.TrimSpace(pair[idx+1:])
		if token == "" || userID == "" || strings.Contains(userID, ":") {
			return nil, fmt.Errorf("malformed tenant entry %d (want token:user_id)", len(out)+1)
		}
		if _, dup := seen[token]; dup {
			return nil, errors.New("duplicate tenant token")
		}
		seen[token] = struct{}{}
		out = append(out, TenantToken{Token: token, UserID: userID, CubeID: userID})
	}
	return out, nil
}

func promptConfigFromEnv(env reader) PromptConfig {
	return PromptConfig{
		TestBurst:            env.int("MEMOSD_PROMPT_TEST_BURST", defaultPromptTestBurst),
		TestRefillPerMin:     env.int("MEMOSD_PROMPT_TEST_REFILL_PER_MIN", defaultPromptTestRefill),
		GenerateBurst:        env.int("MEMOSD_PROMPT_GENERATE_BURST", defaultPromptGenBurst),
		GenerateRefillPerMin: env.int("MEMOSD_PROMPT_GENERATE_REFILL_PER_MIN", defaultPromptGenRefill),
	}
}

func httpConfigFromEnv(env reader) HTTPConfig {
	return HTTPConfig{
		ConsoleAddr:      env.str("MEMOSD_CONSOLE_ADDR", defaultConsoleAddr),
		MCPAddr:          env.str("MEMOSD_MCP_ADDR", defaultMCPAddr),
		PprofAddr:        env.str("MEMOSD_PPROF_ADDR", ""),
		MetricsAddr:      env.str("MEMOSD_METRICS_ADDR", ""),
		MaxBodyBytes:     env.int64("MEMOSD_MAX_BODY_BYTES", defaultMaxBodyBytes),
		WebDir:           strings.TrimSpace(env.str("MEMOSD_WEB_DIR", "")),
		PluginDir:        strings.TrimSpace(env.str("MEMOSD_PLUGIN_DIR", "")),
		PublicServiceURL: strings.TrimRight(strings.TrimSpace(env.str("MEMOSD_PUBLIC_SERVICE_URL", "")), "/"),
		PublicConsoleURL: strings.TrimRight(strings.TrimSpace(env.str("MEMOSD_PUBLIC_CONSOLE_URL", "")), "/"),
		// 默认不放通任何跨域来源：生产必须显式列出，开发用 Vite 代理时同源本就不需要。
		CORSOrigins:       env.list("MEMOSD_CORS_ORIGINS"),
		ReadHeaderTimeout: env.duration("MEMOSD_READ_HEADER_TIMEOUT", defaultReadHeaderTimeout),
		ReadTimeout:       env.duration("MEMOSD_READ_TIMEOUT", defaultReadTimeout),
		ShutdownTimeout:   env.duration("MEMOSD_SHUTDOWN_TIMEOUT", defaultShutdownTimeout),
	}
}

func validatePublicURLs(c HTTPConfig) error {
	if (c.PublicServiceURL == "") != (c.PublicConsoleURL == "") {
		return errors.New("public service and console URLs must both be configured")
	}
	for _, raw := range []string{c.PublicServiceURL, c.PublicConsoleURL} {
		if raw == "" {
			continue
		}
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return errors.New("public URLs must be absolute http(s) URLs without credentials, query or fragment")
		}
	}
	return nil
}

// validateSkill 不回显服务凭据，也不允许把凭据混进 URL。
func (c Config) validateSkill() error {
	if c.Skill.BaseURL == "" && c.Skill.Token == "" {
		return nil
	}
	if c.Skill.BaseURL == "" || c.Skill.Token == "" {
		return errors.New("MEMOS_SKILL_API_BASE_URL and MEMOS_SKILL_API_TOKEN must be set together")
	}
	u, err := url.Parse(c.Skill.BaseURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return errors.New("MEMOS_SKILL_API_BASE_URL must be an HTTP(S) URL without credentials, query or fragment")
	}
	if len(c.Skill.Token) < 32 {
		return errors.New("MEMOS_SKILL_API_TOKEN must be at least 32 characters")
	}
	for _, r := range c.Skill.Token {
		if r <= 32 || r >= 127 {
			return errors.New("MEMOS_SKILL_API_TOKEN must contain only visible ASCII without spaces")
		}
	}
	return nil
}

func skillConfigFromEnv(env reader) SkillConfig {
	return SkillConfig{BaseURL: strings.TrimRight(env.str("MEMOS_SKILL_API_BASE_URL", ""), "/"), Token: env.getenv("MEMOS_SKILL_API_TOKEN")}
}
