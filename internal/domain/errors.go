package domain

import (
	"errors"
	"time"
)

// 领域错误哨兵。transport 用 errors.Is 判断后映射成状态码或错误码，
// 不比较错误字符串。
var (
	ErrNotFound            = errors.New("not found")
	ErrInvalidInput        = errors.New("invalid input")
	ErrUnauthorized        = errors.New("unauthorized")
	ErrForbidden           = errors.New("forbidden")
	ErrConflict            = errors.New("conflict")
	ErrUpstreamUnavailable = errors.New("upstream unavailable")
	ErrUpstreamRejected    = errors.New("upstream rejected")
	// ErrUpstreamDenied 是上游 401/403/404/409：控制面侧配置或路由问题，重试不会自愈。
	// 与 ErrUnauthorized/ErrForbidden 区分，避免把控制面的问题报成调用方凭据错误。
	ErrUpstreamDenied = errors.New("upstream denied")
	ErrDisabled       = errors.New("disabled")
	ErrRateLimited    = errors.New("rate limited")
	// ErrTooLarge 表示请求内容超过上游或控制面的大小上限（HTTP 413）。
	ErrTooLarge = errors.New("too large")
	// ErrModelFailed 与 ErrModelTimeout 是数据面调用大模型的失败与超时。
	// 它们不属于“记忆服务不可用”：模型侧的问题重试可能成功，但不是控制面能自愈的。
	ErrModelFailed  = errors.New("model failed")
	ErrModelTimeout = errors.New("model timeout")
	// ErrQuotaExceeded 表示项目配额或单次上传体积超限，transport 映射为 413。
	ErrQuotaExceeded = errors.New("quota exceeded")
)

// jobReplaySafe 是「重放不会写第二遍」的错误类型。判定口径只有一条：错误是否
// **保证发生在请求被上游接受之前**。校验失败、认证/路由被拒、上游明确拒绝都属于
// 这一类；ErrUpstreamUnavailable（连不上、超时、5xx）可能发生在请求已送达之后，
// 分不清「真失败」和「写成功但响应丢了」，重放会写出第二条记忆。
var jobReplaySafe = map[string]bool{
	ErrNotFound.Error():         true,
	ErrInvalidInput.Error():     true,
	ErrUnauthorized.Error():     true,
	ErrForbidden.Error():        true,
	ErrConflict.Error():         true,
	ErrUpstreamRejected.Error(): true,
	ErrUpstreamDenied.Error():   true,
	// 限流与「空间已停用」同样发生在请求被上游接受之前：前者是上游主动拒收，
	// 后者请求根本没发出去。重试/重放都不会写出第二条。
	ErrRateLimited.Error(): true,
	ErrDisabled.Error():    true,
	ErrTooLarge.Error():    true,
}

// ReplaySafe 判断按任务错误类型名（jobs.last_error）重放死信是否安全。
// 空串或未知类型一律按不安全处理：宁可要求人显式确认，也不静默重复写入。
func ReplaySafe(errName string) bool {
	return jobReplaySafe[errName]
}

// Error 是带操作名与安全消息的领域错误。
//
// Message 可以原样返回给调用方；Err 是内部原因（上游状态、SQL 错误等），
// 只用于日志与 errors.As，不得出现在对外响应里。
type Error struct {
	Kind    error
	Op      string
	Message string
	Err     error
}

// Wrap 构造一个领域错误。message 允许为空，此时 Error() 回退到 Kind。
func Wrap(kind error, op, message string, err error) *Error {
	return &Error{Kind: kind, Op: op, Message: message, Err: err}
}

func (e *Error) Error() string {
	msg := e.Message
	if msg == "" {
		msg = e.Kind.Error()
	}
	if e.Op != "" {
		msg = e.Op + ": " + msg
	}
	return msg
}

// Unwrap 返回内部原因，保留 errors.As 的穿透能力。
func (e *Error) Unwrap() error { return e.Err }

// Is 让 errors.Is(err, ErrUnauthorized) 这类判断不依赖消息文本。
func (e *Error) Is(target error) bool {
	return target != nil && e.Kind == target
}

// KindOf 返回错误命中的哨兵，未命中返回 nil。包装层可以嵌套。
func KindOf(err error) error {
	if err == nil {
		return nil
	}
	for _, kind := range []error{
		ErrNotFound, ErrInvalidInput, ErrUnauthorized, ErrForbidden,
		ErrConflict, ErrUpstreamUnavailable, ErrUpstreamRejected, ErrUpstreamDenied,
		ErrDisabled, ErrRateLimited, ErrTooLarge, ErrModelFailed, ErrModelTimeout, ErrQuotaExceeded,
	} {
		if errors.Is(err, kind) {
			return kind
		}
	}
	return nil
}

// Invalid 是构造参数错误的便捷函数。
func Invalid(op, message string) *Error {
	return Wrap(ErrInvalidInput, op, message, nil)
}

// NotFound 是资源不存在的便捷函数。
func NotFound(op, message string) *Error {
	return Wrap(ErrNotFound, op, message, nil)
}

// TooLarge 是内容超限的便捷函数；transport 映射成 413。
func TooLarge(op, message string) *Error {
	return Wrap(ErrTooLarge, op, message, nil)
}

// VersionConflictError 是保存提示词时客户端所见的版本已经过期的错误。
// CurrentVersionID 是当前生效版本，transport 把它放进响应的 data，
// 页面据此展示“草稿与当前版本的对照”，而不是只弹一句冲突。
type VersionConflictError struct {
	CurrentVersionID int64
	// CurrentVersionNo 是同一槽位的展示序号（从 1 开始），与 VersionID 一起下发，
	// 页面显示“当前是第几版”用它，不用全局编号。
	CurrentVersionNo int64
}

func (e *VersionConflictError) Error() string {
	return "提示词已被他人更新，请刷新后再保存"
}

// Is 让 errors.Is(err, ErrConflict) 成立，不影响错误类型名（审计记 conflict）。
func (e *VersionConflictError) Is(target error) bool { return target == ErrConflict }

// BusyError 是限流错误。Wait 是建议的重试间隔，transport 用它同时写
// Retry-After 头与 data.retryAfterSec；控制面的令牌桶与上游 busy 共用它。
type BusyError struct {
	Wait    time.Duration
	Message string
}

func (e *BusyError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return "操作太频繁，请稍后重试"
}

// Is 让 errors.Is(err, ErrRateLimited) 成立。
func (e *BusyError) Is(target error) bool { return target == ErrRateLimited }

// SafeMessage 返回可以原样给调用方的消息：领域错误取 Message（没有则取哨兵文本），
// 不带 Op 前缀；其他错误取 err.Error()。
func SafeMessage(err error) string {
	if de, ok := errors.AsType[*Error](err); ok {
		if de.Message != "" {
			return de.Message
		}
		return de.Kind.Error()
	}
	return err.Error()
}
