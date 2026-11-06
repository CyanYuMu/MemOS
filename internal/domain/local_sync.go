package domain

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"slices"
	"strings"
	"time"
)

// LocalLayer 是本地插件的数据层级。四层各有独立生命周期，云端只保存镜像。
type LocalLayer string

const (
	LocalLayerTrace      LocalLayer = "trace"
	LocalLayerPolicy     LocalLayer = "policy"
	LocalLayerWorldModel LocalLayer = "world_model"
)

// Valid 表示层级是四个已知值之一。
func (l LocalLayer) Valid() bool {
	switch l {
	case LocalLayerTrace, LocalLayerPolicy, LocalLayerWorldModel:
		return true
	default:
		return false
	}
}

// ParseLocalLayer 把请求里的层级字符串转成枚举；未知值返回 false。
func ParseLocalLayer(raw string) (LocalLayer, bool) {
	l := LocalLayer(strings.TrimSpace(raw))
	return l, l.Valid()
}

// LocalTraceMode 决定 trace 层是否允许上传原始正文。
type LocalTraceMode string

const (
	// LocalTraceModeSummary 只允许摘要级字段；出现原始正文整行拒绝。
	LocalTraceModeSummary LocalTraceMode = "summary"
	// LocalTraceModeFull 允许原始 prompt 与工具 IO；服务端不承诺脱敏。
	LocalTraceModeFull LocalTraceMode = "full"
)

func (m LocalTraceMode) Valid() bool {
	return m == LocalTraceModeSummary || m == LocalTraceModeFull
}

// localTraceSummaryKeys 是 summary 模式下 trace payload 的唯一白名单。
var localTraceSummaryKeys = map[string]struct{}{
	"summary":   {},
	"tags":      {},
	"toolNames": {},
	"toolCount": {},
	"ts":        {},
}

// ValidateTraceSummaryPayload 校验 summary 模式的 trace payload。
// 只允许五个摘要字段，任何其它键（原始 prompt、工具入参/出参等）都视为违规。
func ValidateTraceSummaryPayload(payload map[string]any) error {
	for key := range payload {
		if _, ok := localTraceSummaryKeys[key]; !ok {
			return Invalid("local_sync", "summary 模式下 trace payload 不允许字段 "+key)
		}
	}
	return nil
}

// LocalDeviceStatus 是设备令牌的生命周期状态。
type LocalDeviceStatus string

const (
	LocalDeviceActive  LocalDeviceStatus = "active"
	LocalDeviceRevoked LocalDeviceStatus = "revoked"
)

// LocalCodeStatus 是设备授权码的生命周期。授权码短时存在，与设备令牌分开。
type LocalCodeStatus string

const (
	LocalCodePending  LocalCodeStatus = "pending"
	LocalCodeApproved LocalCodeStatus = "approved"
	LocalCodeDenied   LocalCodeStatus = "denied"
	LocalCodeConsumed LocalCodeStatus = "consumed"
)

// LocalBatchStatus 是上传批次的状态。只有 pending 与 committed 两种终态。
type LocalBatchStatus string

const (
	LocalBatchPending   LocalBatchStatus = "pending"
	LocalBatchCommitted LocalBatchStatus = "committed"
)

// 转存状态：queued/running 是进行中，succeeded/failed 是终态。
const (
	LocalPromoteQueued    = "queued"
	LocalPromoteRunning   = "running"
	LocalPromoteSucceeded = "succeeded"
	LocalPromoteFailed    = "failed"
)

const (
	// LocalSyncTokenPrefix 是设备令牌明文前缀，用于在哈希前快速拒绝无关 Bearer。
	LocalSyncTokenPrefix = "memosls_"
	// LocalManualDeviceID 是手工上传的合成设备 ID，不出现在设备列表。
	LocalManualDeviceID = "manual"
	// LocalRowCountLimit 是四个层级的总行数上限（单项目）。
	LocalRowCountLimit int64 = 500_000
	// LocalQuotaBytesLimit 是单项目镜像存储上限（2 GiB）。
	LocalQuotaBytesLimit int64 = 2 << 30
	// LocalRowPayloadLimit 是单行 payload 序列化后的字节上限（256 KiB）。
	LocalRowPayloadLimit int = 256 << 10
	// LocalChunkBytesLimit 是单块 gzip 后上限（4 MiB）。
	LocalChunkBytesLimit int64 = 4 << 20
	// LocalBatchBytesLimit 是单批 gzip 后上限（64 MiB）。
	LocalBatchBytesLimit int64 = 64 << 20
	// LocalBatchDecompressedLimit 是一次 commit 全部块解压后的总量上限（256 MiB）：
	// 单块上限挡不住大量高压缩比小块叠加。
	LocalBatchDecompressedLimit int64 = 256 << 20
	// LocalChunkSeqMax 是分块序号上限，防止客户端用巨大 seq 撑大索引。
	LocalChunkSeqMax = 1023
)

// LocalDevice 是已绑定的插件设备。TokenHash 是设备令牌明文的 SHA-256。
type LocalDevice struct {
	ID         string
	AccountID  string
	TenantID   string
	DeviceID   string
	Label      string
	AgentKind  string
	ProfileID  string
	TokenHash  [32]byte
	Status     LocalDeviceStatus
	CreatedAt  time.Time
	LastSeenAt *time.Time
	RevokedAt  *time.Time
}

// Expired 判断设备令牌是否已超过滑动过期窗口。last_seen_at 为空时以 created_at 为起点。
func (d LocalDevice) Expired(now time.Time, ttl time.Duration) bool {
	if ttl <= 0 {
		return false
	}
	base := d.CreatedAt
	if d.LastSeenAt != nil {
		base = *d.LastSeenAt
	}
	return now.After(base.Add(ttl))
}

// Active 表示设备可接受请求：未吊销且未滑动过期。
func (d LocalDevice) Active(now time.Time, ttl time.Duration) bool {
	return d.Status == LocalDeviceActive && d.RevokedAt == nil && !d.Expired(now, ttl)
}

// LocalDeviceCode 是 OAuth Device Flow 风格的短时授权码。
type LocalDeviceCode struct {
	CodeHash    [32]byte
	UserCode    string
	DeviceLabel string
	AgentKind   string
	Status      LocalCodeStatus
	AccountID   string
	TenantID    string
	ExpiresAt   time.Time
	CreatedAt   time.Time
	DecidedAt   *time.Time
}

// LocalSyncBatch 是一次上传批次。Layers 是创建时声明的允许层级。
type LocalSyncBatch struct {
	ID            string
	TenantID      string
	DeviceID      string
	Status        LocalBatchStatus
	Layers        []LocalLayer
	Manifest      map[string]int
	ManifestHash  string
	TraceMode     LocalTraceMode
	ReceivedBytes int64
	Accepted      int64
	Rejected      int64
	Error         string
	ResultJSON    string
	Cursor        time.Time
	CreatedAt     time.Time
	CommittedAt   *time.Time
}

// Expired 表示未提交批次已超过 TTL。
func (b LocalSyncBatch) Expired(now time.Time, ttl time.Duration) bool {
	return b.Status == LocalBatchPending && ttl > 0 && now.After(b.CreatedAt.Add(ttl))
}

// Allows 表示层级是否在批次声明里。
func (b LocalSyncBatch) Allows(layer LocalLayer) bool {
	return slices.Contains(b.Layers, layer)
}

// LocalRow 是一条已通过格式校验、待落库的镜像行。
type LocalRow struct {
	Layer       LocalLayer
	SourceID    string
	ContentHash string
	// ContentDigest 是去掉 updatedAt 的内容摘要（旧客户端没有，可为空）。
	// 与 ContentHash 不同，它不随 embedding 重试刷新时间戳而变化，墓碑与幂等
	// 判断优先用它，避免“时间戳变一行、内容没变”被当成新内容复活已删除的行。
	ContentDigest   string
	Title           string
	Summary         string
	Payload         map[string]any
	PayloadGzip     []byte
	PayloadBytes    int64
	SourceUpdatedAt time.Time
}

// LocalRejectedReason 是单行被拒的原因，返回给客户端定位具体行。
type LocalRejectedReason struct {
	Layer  string `json:"layer"`
	ID     string `json:"id"`
	Reason string `json:"reason"`
}

// LocalCommitResult 是 commit 的结果，也是重放时原样返回的首次结果。
type LocalCommitResult struct {
	Status          string                `json:"status"`
	Accepted        int64                 `json:"accepted"`
	Rejected        int64                 `json:"rejected"`
	RejectedReasons []LocalRejectedReason `json:"rejectedReasons"`
	Cursor          time.Time             `json:"cursor"`
}

// LocalQuota 是项目级配额与已用量。已软删除的行不占配额。
//
// UsedBytes/UsedRows 只统计已落库的镜像行；PendingBytes/PendingRows 是尚未提交
// 批次真实占用的分块字节与 manifest 声明行数。服务端在写分块与建批次时把两者
// 相加后再和上限比较，所以对外展示也必须分开给出，否则会出现“界面显示还有空间、
// 上传却返回 413”的矛盾。
type LocalQuota struct {
	UsedBytes    int64
	LimitBytes   int64
	UsedRows     int64
	LimitRows    int64
	PendingBytes int64
	PendingRows  int64
}

// LocalState 是插件恢复同步所需的游标与配额。
type LocalState struct {
	DeviceID       string
	TenantID       string
	LastBatchID    string
	LastAcceptedAt *time.Time
	Quota          LocalQuota
}

// LocalMemory 是本地记忆镜像。Payload 是 gzip 压缩后的原始 JSON。
type LocalMemory struct {
	ID               string
	TenantID         string
	DeviceID         string
	DeviceLabel      string
	Layer            LocalLayer
	SourceID         string
	ContentHash      string
	Title            string
	Summary          string
	Payload          []byte
	SourceUpdatedAt  time.Time
	DeletedAt        *time.Time
	PromotedMemoryID string
	PromoteJobID     string
	PromoteStatus    string
	PromoteError     string
	SyncedAt         time.Time
}

// Deleted 表示镜像已被控制台删除（只影响副本，不传播到本地）。
func (m LocalMemory) Deleted() bool { return m.DeletedAt != nil }

// LocalMemoryFilter 是控制台列表查询条件。列表查询不读取 payload。
// StartTime/EndTime 作用于 source_updated_at，零值表示不设边界。
type LocalMemoryFilter struct {
	TenantID  string
	Layer     string
	DeviceID  string
	Query     string
	StartTime time.Time
	EndTime   time.Time
	Page      int
	PageSize  int
}

// LocalMemorySummary 是项目本地镜像的治理视图：分层计数、转存状态与时间范围。
// 只统计控制面副本，不代表本地插件或云端记忆的真实总量。
type LocalMemorySummary struct {
	TenantID       string
	Layers         map[string]int64
	Total          int64
	Deleted        int64
	Promoted       int64
	PromotePending int64
	PromoteFailed  int64
	OldestSyncedAt *time.Time
	NewestSyncedAt *time.Time
	Quota          LocalQuota
}

// LocalPromoteFailure 是转存请求里单条未入队的原因。
type LocalPromoteFailure struct {
	ID      string `json:"id"`
	Message string `json:"message"`
}

// LocalPromoteResult 是批量转存结果。已转存或转存中的 id 计入 Queued（幂等）。
type LocalPromoteResult struct {
	Queued []string              `json:"queued"`
	Failed []LocalPromoteFailure `json:"failed"`
}

// NewLocalSyncToken 生成 32 字节随机的设备令牌（hex，带固定前缀）。
func NewLocalSyncToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", Wrap(ErrInvalidInput, "local_sync", "无法生成设备令牌", err)
	}
	return LocalSyncTokenPrefix + hex.EncodeToString(buf), nil
}

// HashLocalSyncToken 返回设备令牌的 SHA-256；库里只存这个值。
func HashLocalSyncToken(raw string) [32]byte { return sha256.Sum256([]byte(raw)) }

// IsLocalSyncToken 只做前缀判定，避免对任意 Bearer 做哈希与查库。
func IsLocalSyncToken(raw string) bool {
	return strings.HasPrefix(strings.TrimSpace(raw), LocalSyncTokenPrefix)
}

// localUserCodeAlphabet 去掉 I/O/0/1，避免用户抄错。
const localUserCodeAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

const localUserCodeLength = 8

// NewDeviceUserCode 生成 XXXX-XXXX 形式的用户授权码。
func NewDeviceUserCode() (string, error) {
	out := make([]byte, 0, localUserCodeLength+1)
	buf := make([]byte, localUserCodeLength)
	if _, err := rand.Read(buf); err != nil {
		return "", Wrap(ErrInvalidInput, "local_sync", "无法生成用户授权码", err)
	}
	for i, b := range buf {
		if i == localUserCodeLength/2 {
			out = append(out, '-')
		}
		out = append(out, localUserCodeAlphabet[int(b)%len(localUserCodeAlphabet)])
	}
	return string(out), nil
}

// NormalizeUserCode 接受带或不带连字符、任意大小写的用户输入，
// 统一成库里存储的 XXXX-XXXX 大写形式。
func NormalizeUserCode(raw string) string {
	raw = strings.ToUpper(strings.TrimSpace(raw))
	raw = strings.ReplaceAll(raw, "-", "")
	raw = strings.ReplaceAll(raw, " ", "")
	if len(raw) != localUserCodeLength {
		return ""
	}
	return raw[:localUserCodeLength/2] + "-" + raw[localUserCodeLength/2:]
}

// NewLocalDeviceID 生成设备公开 ID；与令牌哈希分开，吊销与展示都用它。
func NewLocalDeviceID() string { return NewID() }

// LocalBatchExpired 是批次过期判定的便捷函数，供 store 与 service 共用。
func LocalBatchExpired(b LocalSyncBatch, now time.Time, ttl time.Duration) bool {
	return b.Expired(now, ttl)
}
