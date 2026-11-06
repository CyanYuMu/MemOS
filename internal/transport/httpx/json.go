package httpx

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// 控制面统一错误码（与 Dashboard 的 cloud 错误码表一致）。
// 码位按 HTTP 状态的首三位扩展：40000 参数错误、40900 冲突、42900 限流、
// 50300 能力未启用或上游不可用，前端据 code 而不是仅凭 status 做分支。
const (
	CodeOK               = 0
	CodeBadRequest       = 40000
	CodeNotFound         = 40001
	CodeMethodNotAllowed = 40500
	CodeUnauthorized     = 40100
	CodeForbidden        = 40300
	CodeConflict         = 40900
	// CodePayloadTooLarge 是请求体或正文超过上限（HTTP 413）。既有码表里
	// 没有“请求体过大”的专门码，40000（参数错误）会让前端当成用户填错，
	// 所以单独给一个。
	CodePayloadTooLarge = 41300
	CodeRateLimited     = 42900
	CodeInternal        = 50000
	CodeNotImplemented  = 50100
	// CodeModelFailed / CodeModelTimeout 是数据面调用大模型失败与超时。
	// 它们不是“能力未启用”，也与控制面内部错误区分：调用方可以稍后重试。
	CodeModelFailed      = 50200
	CodeUpstreamRejected = 50143
	CodeModelTimeout     = 50400
	CodeUnavailable      = 50300
)

// Envelope 是 Dashboard 与 REST 共用的响应外壳。
type Envelope struct {
	Code    int    `json:"code"`
	Data    any    `json:"data"`
	Message string `json:"message"`
}

// 解码错误。transport 用 errors.Is 判断后映射状态码。
var (
	ErrBodyTooLarge = errors.New("request body too large")
	ErrBadJSON      = errors.New("request body is not valid JSON")
	ErrNotJSON      = errors.New("write endpoint requires application/json")
)

// WriteJSON 写 JSON 响应。
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		// 响应已经开始写出，无法再改状态码，只能放弃。
		return
	}
}

// OK 写成功信封：{code:0,data:...,message:"success"}。
func OK(w http.ResponseWriter, data any) {
	WriteJSON(w, http.StatusOK, Envelope{Code: CodeOK, Data: data, Message: "success"})
}

// Fail 写错误信封：data 固定 null。
func Fail(w http.ResponseWriter, status, code int, message string) {
	FailData(w, status, code, message, nil)
}

// FailData 写带业务数据的错误信封。目前只有提示词工作台用：版本冲突要把
// 当前版本号、限流要把等待秒数放进 data，页面据此展示对照与倒计时。
func FailData(w http.ResponseWriter, status, code int, message string, data any) {
	WriteJSON(w, status, Envelope{Code: code, Data: data, Message: message})
}

// NotFoundJSON 是各角色 mux 的 "/" 兜底：未知路径也返回 JSON 信封，
// 而不是 Go 默认的 text/plain "404 page not found"。
func NotFoundJSON(w http.ResponseWriter) {
	Fail(w, http.StatusNotFound, CodeNotFound, "接口不存在")
}

// SetRetryAfter 把限流窗口写成 Retry-After（秒，向上取整，至少 1）。
//
// 窗口是“最长需要等待多久”的上限，不是精确剩余时间：节流计数按滑动窗口
// 逐步过期，调用方拿它做退避即可。window<=0 表示调用方没有窗口信息，
// 此时不设置该头，保持旧行为。
func SetRetryAfter(w http.ResponseWriter, window time.Duration) {
	if window <= 0 {
		return
	}
	seconds := max(int64((window+time.Second-1)/time.Second), 1)
	w.Header().Set("Retry-After", strconv.FormatInt(seconds, 10))
}

// DecodeJSON 读取并解析请求体。
//
// 先按字节读完再一次性解码：分片逐个转字符串会把跨片的 UTF-8 汉字打成 U+FFFD。
// MaxBody 中间件负责体积上限，这里只把超限错误翻译成 ErrBodyTooLarge。
func DecodeJSON(r *http.Request, dst any) error {
	if r.Body == nil {
		return nil
	}
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		if IsBodyTooLarge(err) {
			return ErrBodyTooLarge
		}
		return err
	}
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil
	}
	if err := json.Unmarshal(raw, dst); err != nil {
		return ErrBadJSON
	}
	return nil
}

// RequireJSON 要求写接口带 body 时使用 application/json。
//
// CORS 白名单只限制“读”；跨站简单请求（text/plain）不触发预检，
// 因此写接口还要自己拒绝非 JSON 的 body（与 BFF 的 assertWriteAllowed 一致）。
func RequireJSON(r *http.Request) error {
	// 服务端请求里 Content-Length 会被 net/http 收进 ContentLength（0 = 无 body，
	// -1 = 分块或未知），Header 里通常取不到。
	hasBody := r.ContentLength != 0 || len(r.TransferEncoding) > 0
	if !hasBody {
		return nil
	}
	ct := strings.ToLower(r.Header.Get("Content-Type"))
	if !strings.Contains(ct, "application/json") {
		return ErrNotJSON
	}
	return nil
}
