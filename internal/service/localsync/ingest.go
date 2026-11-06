package localsync

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	maxBatchIDLen   = 128
	minBatchIDLen   = 8
	maxSourceIDLen  = 512
	maxTitleRunes   = 512
	maxSummaryRunes = 8192
	// maxRowLineBytes 是单行 JSONL 的硬上限：行内 payload 上限之外再给元数据留余量。
	maxRowLineExtra = 64 << 10
	maxEpochMillis  = int64(253402300799999) // 9999-12-31T23:59:59.999Z
)

var (
	errRowPayloadTooLarge   = errors.New("row payload too large")
	errDecompressedTooLarge = errors.New("decompressed chunk too large")
)

// CreateBatchInput 是 POST /api/local-sync/batches 的入参。
type CreateBatchInput struct {
	BatchID   string
	Layers    []string
	Manifest  map[string]int
	TraceMode string
}

// BatchGrant 是创建批次的响应。
type BatchGrant struct {
	BatchID   string
	ExpiresAt time.Time
}

// ManualBundle 是手工上传的 bundle，行格式与 JSONL 行一致。
type ManualBundle struct {
	BatchID   string
	Layers    []string
	Manifest  map[string]int
	TraceMode string
	Rows      []json.RawMessage
}

// State 返回设备的游标、上次提交与配额。
func (s *Service) State(ctx context.Context, id DeviceIdentity) (domain.LocalState, error) {
	state, err := s.store.LocalState(ctx, id.TenantID, id.DeviceID)
	if err != nil {
		return domain.LocalState{}, err
	}
	quota, err := s.quotaView(ctx, id.TenantID)
	if err != nil {
		return domain.LocalState{}, err
	}
	state.Quota = quota
	return state, nil
}

// CreateBatch 校验声明并登记一个待上传批次。
func (s *Service) CreateBatch(ctx context.Context, id DeviceIdentity, in CreateBatchInput) (BatchGrant, error) {
	if err := s.allowWrite(id); err != nil {
		return BatchGrant{}, err
	}
	batchID := trimmed(in.BatchID)
	if !validBatchID(batchID) {
		return BatchGrant{}, domain.Invalid("local_sync", "batchId 必须是 8..128 位字母、数字、点、下划线或连字符")
	}
	layers, manifest, traceMode, err := normalizeDeclaration(in.Layers, in.Manifest, in.TraceMode)
	if err != nil {
		return BatchGrant{}, err
	}
	now := s.now()
	s.purgeBatches(ctx, now)
	// 幂等重试要返回 409，而不是先被配额拦住：客户端在响应丢失后会重发同一
	// batchId，此时该批次的 manifest 已经计入 pending，再加一次就会误报 413。
	// 判重、配额与插入在 store 的同一个写事务里完成，避免并发建批次突破行配额。
	batch := domain.LocalSyncBatch{
		ID:           batchID,
		TenantID:     id.TenantID,
		DeviceID:     id.DeviceID,
		Status:       domain.LocalBatchPending,
		Layers:       layers,
		Manifest:     manifest,
		ManifestHash: manifestHash(manifest),
		TraceMode:    traceMode,
		CreatedAt:    now,
	}
	if err := s.store.CreateBatchWithinQuota(ctx, batch, s.opts.QuotaBytes, s.opts.QuotaRows); err != nil {
		return BatchGrant{}, err
	}
	return BatchGrant{BatchID: batchID, ExpiresAt: now.Add(s.opts.BatchTTL)}, nil
}

// PutChunk 保存一块 gzip JSONL。同一 seq 重发覆盖，累计字节按差值调整。
func (s *Service) PutChunk(ctx context.Context, id DeviceIdentity, batchID string, seq int, body []byte) (int64, error) {
	if err := s.allowWrite(id); err != nil {
		return 0, err
	}
	if seq < 0 || seq > domain.LocalChunkSeqMax {
		return 0, domain.Invalid("local_sync", fmt.Sprintf("seq 必须在 0..%d", domain.LocalChunkSeqMax))
	}
	if len(body) == 0 {
		return 0, domain.Invalid("local_sync", "分块不能为空")
	}
	if int64(len(body)) > s.opts.MaxChunkBytes {
		return 0, domain.Wrap(domain.ErrQuotaExceeded, "local_sync",
			fmt.Sprintf("单块上限 %d 字节", s.opts.MaxChunkBytes), nil)
	}
	batch, err := s.store.GetBatch(ctx, id.TenantID, id.DeviceID, trimmed(batchID))
	if err != nil {
		return 0, err
	}
	if batch.Status != domain.LocalBatchPending {
		return 0, domain.Wrap(domain.ErrConflict, "local_sync", "批次已提交，不能继续上传", nil)
	}
	if batch.Expired(s.now(), s.opts.BatchTTL) {
		return 0, domain.NotFound("local_sync", "批次已过期，请重新创建")
	}
	return s.store.PutChunk(ctx, id.TenantID, id.DeviceID, batch.ID, seq, body, s.opts.MaxBatchBytes, s.opts.QuotaBytes, s.now())
}

// CommitBatch 解析全部块并落库；重放已提交批次时返回首次结果。
func (s *Service) CommitBatch(ctx context.Context, id DeviceIdentity, batchID string) (domain.LocalCommitResult, error) {
	if err := s.allowWrite(id); err != nil {
		return domain.LocalCommitResult{}, err
	}
	batch, err := s.store.GetBatch(ctx, id.TenantID, id.DeviceID, trimmed(batchID))
	if err != nil {
		return domain.LocalCommitResult{}, err
	}
	if batch.Status == domain.LocalBatchCommitted {
		return replayResult(batch)
	}
	if batch.Expired(s.now(), s.opts.BatchTTL) {
		return domain.LocalCommitResult{}, domain.NotFound("local_sync", "批次已过期，请重新创建")
	}
	result, err := s.applyBatch(ctx, id, batch)
	if err != nil {
		return domain.LocalCommitResult{}, err
	}
	now := s.now()
	if err := s.store.FinishBatch(ctx, id.TenantID, id.DeviceID, batch.ID, result, now); err != nil {
		return s.commitConflict(ctx, id, batch, err)
	}
	s.auditCommit(ctx, id, batch, result)
	return result, nil
}

// commitConflict 处理并发提交撞车：同批次已提交按重放返回，其它冲突原样上抛。
func (s *Service) commitConflict(ctx context.Context, id DeviceIdentity, batch domain.LocalSyncBatch, err error) (domain.LocalCommitResult, error) {
	if !errors.Is(err, domain.ErrConflict) {
		return domain.LocalCommitResult{}, err
	}
	reloaded, getErr := s.store.GetBatch(ctx, id.TenantID, id.DeviceID, batch.ID)
	if getErr != nil {
		return domain.LocalCommitResult{}, getErr
	}
	if reloaded.Status == domain.LocalBatchCommitted {
		return replayResult(reloaded)
	}
	return domain.LocalCommitResult{}, err
}

func (s *Service) applyBatch(ctx context.Context, id DeviceIdentity, batch domain.LocalSyncBatch) (domain.LocalCommitResult, error) {
	seqs, err := s.store.ListChunkSeqs(ctx, id.TenantID, id.DeviceID, batch.ID)
	if err != nil {
		return domain.LocalCommitResult{}, err
	}
	seen := make(map[domain.LocalLayer]int, len(batch.Layers))
	result := domain.LocalCommitResult{
		Status:          string(domain.LocalBatchCommitted),
		RejectedReasons: make([]domain.LocalRejectedReason, 0),
	}
	// 所有分块先解析完，再一次性落库：配额检查与写入必须在同一事务里，
	// 否则“第 0 块写入成功、第 1 块超限”会留下半批镜像，与文档承诺不符。
	allRows := make([]domain.LocalRow, 0, len(seqs)*64)
	var decompressed int64
	for _, seq := range seqs {
		body, err := s.store.GetChunk(ctx, id.TenantID, id.DeviceID, batch.ID, seq)
		if err != nil {
			return domain.LocalCommitResult{}, err
		}
		rows, rejected, reasons, cursor, read, err := s.parseChunk(body, &batch, seen)
		if err != nil {
			return domain.LocalCommitResult{}, err
		}
		decompressed += read
		limit := s.opts.MaxBatchDecompressed
		if limit <= 0 {
			limit = domain.LocalBatchDecompressedLimit
		}
		if decompressed > limit {
			// 单块 32 MiB 的防护挡不住“1024 个高压缩比小块”：整批解压总量
			// 必须封顶，否则 allRows 会把进程内存吃光。
			return domain.LocalCommitResult{}, domain.Invalid("local_sync",
				fmt.Sprintf("整批解压后超过 %d 字节", limit))
		}
		result.Rejected += rejected
		result.RejectedReasons = appendCapped(result.RejectedReasons, reasons)
		allRows = append(allRows, rows...)
		if cursor.After(result.Cursor) {
			result.Cursor = cursor
		}
	}
	if len(allRows) > 0 {
		applied, unchanged, err := s.store.ApplyRows(ctx, id.TenantID, id.DeviceID, allRows, s.limits())
		if err != nil {
			return domain.LocalCommitResult{}, err
		}
		result.Accepted = applied + unchanged
	}
	return result, nil
}

func (s *Service) parseChunk(body []byte, batch *domain.LocalSyncBatch, seen map[domain.LocalLayer]int) ([]domain.LocalRow, int64, []domain.LocalRejectedReason, time.Time, int64, error) {
	gz, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return nil, 0, nil, time.Time{}, 0, domain.Invalid("local_sync", "分块不是合法的 gzip")
	}
	defer func() { _ = gz.Close() }()

	counter := &maxReader{r: gz, max: maxDecompressedChunk}
	reader := bufio.NewReaderSize(counter, 64<<10)
	var (
		rows     []domain.LocalRow
		rejected int64
		reasons  []domain.LocalRejectedReason
		cursor   time.Time
	)
	maxLine := s.opts.MaxRowBytes + maxRowLineExtra
	for {
		line, tooLong, err := readLine(reader, maxLine)
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			if errors.Is(err, errDecompressedTooLarge) {
				return nil, 0, nil, time.Time{}, counter.n, domain.Invalid("local_sync", "分块解压后超过上限")
			}
			return nil, 0, nil, time.Time{}, counter.n, domain.Invalid("local_sync", "分块读取失败")
		}
		if tooLong {
			rejected++
			reasons = appendCapped(reasons, []domain.LocalRejectedReason{{Reason: "单行超过上限"}})
			continue
		}
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		row, reason := s.parseWireRow(line, batch, seen)
		if reason != "" {
			rejected++
			reasons = appendCapped(reasons, []domain.LocalRejectedReason{{
				Layer: string(row.Layer), ID: row.SourceID, Reason: reason,
			}})
			continue
		}
		rows = append(rows, row)
		if row.SourceUpdatedAt.After(cursor) {
			cursor = row.SourceUpdatedAt
		}
	}
	return rows, rejected, reasons, cursor, counter.n, nil
}

// parseWireRow 解析并校验单行。返回非空 reason 表示只拒绝该行，不影响整批。
func (s *Service) parseWireRow(line []byte, batch *domain.LocalSyncBatch, seen map[domain.LocalLayer]int) (domain.LocalRow, string) {
	var wire wireRow
	if err := json.Unmarshal(line, &wire); err != nil {
		return domain.LocalRow{}, "JSON 行格式不正确"
	}
	layer, ok := domain.ParseLocalLayer(wire.Layer)
	if !ok {
		return domain.LocalRow{}, "未知 layer"
	}
	if !batch.Allows(layer) {
		return domain.LocalRow{}, "layer 未在批次中声明"
	}
	seen[layer]++
	if seen[layer] > batch.Manifest[string(layer)] {
		return domain.LocalRow{Layer: layer, SourceID: trimmed(wire.ID)}, "超出 manifest 声明的行数"
	}
	sourceID := trimmed(wire.ID)
	if sourceID == "" {
		return domain.LocalRow{Layer: layer}, "缺少 id"
	}
	if len(sourceID) > maxSourceIDLen {
		return domain.LocalRow{Layer: layer, SourceID: sourceID[:maxSourceIDLen]}, "id 过长"
	}
	contentHash, ok := normalizeContentHash(wire.ContentHash)
	if !ok {
		return domain.LocalRow{Layer: layer, SourceID: sourceID}, "contentHash 必须是 sha256:<64 hex>"
	}
	contentDigest := ""
	if raw := trimmed(wire.ContentDigest); raw != "" {
		digest, ok := normalizeContentHash(raw)
		if !ok {
			return domain.LocalRow{Layer: layer, SourceID: sourceID}, "contentDigest 必须是 sha256:<64 hex>"
		}
		contentDigest = digest
	}
	updatedAt, ok := parseEpochMillis(wire.UpdatedAt.String())
	if !ok {
		return domain.LocalRow{Layer: layer, SourceID: sourceID}, "updatedAt 必须是正数 epoch 毫秒"
	}
	title, summary, reason := wireText(wire)
	if reason != "" {
		return domain.LocalRow{Layer: layer, SourceID: sourceID}, reason
	}
	payload, reason := decodePayload(wire.Payload)
	if reason != "" {
		return domain.LocalRow{Layer: layer, SourceID: sourceID}, reason
	}
	if reason := tracePayloadRejection(batch.TraceMode, layer, payload); reason != "" {
		return domain.LocalRow{Layer: layer, SourceID: sourceID}, reason
	}
	compressed, plainBytes, err := encodePayload(payload, s.opts.MaxRowBytes)
	if err != nil {
		if errors.Is(err, errRowPayloadTooLarge) {
			return domain.LocalRow{Layer: layer, SourceID: sourceID},
				fmt.Sprintf("单行 payload 超过 %d 字节", s.opts.MaxRowBytes)
		}
		return domain.LocalRow{Layer: layer, SourceID: sourceID}, "payload 无法序列化"
	}
	return domain.LocalRow{
		Layer:           layer,
		SourceID:        sourceID,
		ContentHash:     contentHash,
		ContentDigest:   contentDigest,
		Title:           title,
		Summary:         summary,
		Payload:         payload,
		PayloadGzip:     compressed,
		PayloadBytes:    plainBytes,
		SourceUpdatedAt: time.UnixMilli(updatedAt).UTC(),
	}, ""
}

// wireText 截断并校验标题/摘要长度；返回非空 reason 表示该行被拒。
func wireText(wire wireRow) (string, string, string) {
	title := trimmed(wire.Title)
	if len([]rune(title)) > maxTitleRunes {
		return "", "", "title 过长"
	}
	summary := trimmed(wire.Summary)
	if len([]rune(summary)) > maxSummaryRunes {
		return "", "", "summary 过长"
	}
	return title, summary, ""
}

// tracePayloadRejection 在 summary 模式下拒绝 trace 行里任何未声明的字段，
// 尤其是原始 prompt / 工具 IO。full 模式不做该限制。
func tracePayloadRejection(mode domain.LocalTraceMode, layer domain.LocalLayer, payload map[string]any) string {
	if mode != domain.LocalTraceModeSummary || layer != domain.LocalLayerTrace {
		return ""
	}
	err := domain.ValidateTraceSummaryPayload(payload)
	if err == nil {
		return ""
	}
	message := "trace payload 含未经声明的字段"
	var de *domain.Error
	if errors.As(err, &de) && de.Message != "" {
		message = de.Message
	}
	return message
}

// Manual 处理浏览器会话的手工上传，落合成设备 manual，复用同一套校验与配额。
func (s *Service) Manual(ctx context.Context, p domain.Principal, projectID string, bundle ManualBundle) (domain.LocalCommitResult, error) {
	projectID = trimmed(projectID)
	if projectID == "" {
		return domain.LocalCommitResult{}, domain.Invalid("local_sync", "缺少 projectId")
	}
	if err := p.RequireCap(projectID, domain.CapLocalSync); err != nil {
		return domain.LocalCommitResult{}, err
	}
	if !s.writes.allow("manual:" + p.AccountID) {
		return domain.LocalCommitResult{}, domain.Wrap(domain.ErrRateLimited, "local_sync", "请求过于频繁，请稍后再试", nil)
	}
	layers, manifest, traceMode, err := normalizeDeclaration(bundle.Layers, bundle.Manifest, bundle.TraceMode)
	if err != nil {
		return domain.LocalCommitResult{}, err
	}
	batch := domain.LocalSyncBatch{
		ID:        trimmed(bundle.BatchID),
		TenantID:  projectID,
		DeviceID:  domain.LocalManualDeviceID,
		Layers:    layers,
		Manifest:  manifest,
		TraceMode: traceMode,
	}
	seen := make(map[domain.LocalLayer]int, len(layers))
	result := domain.LocalCommitResult{
		Status:          string(domain.LocalBatchCommitted),
		RejectedReasons: make([]domain.LocalRejectedReason, 0),
	}
	rows := make([]domain.LocalRow, 0, len(bundle.Rows))
	for _, raw := range bundle.Rows {
		row, reason := s.parseWireRow(raw, &batch, seen)
		if reason != "" {
			result.Rejected++
			result.RejectedReasons = appendCapped(result.RejectedReasons, []domain.LocalRejectedReason{{
				Layer: string(row.Layer), ID: row.SourceID, Reason: reason,
			}})
			continue
		}
		rows = append(rows, row)
		if row.SourceUpdatedAt.After(result.Cursor) {
			result.Cursor = row.SourceUpdatedAt
		}
	}
	if len(rows) > 0 {
		applied, unchanged, err := s.store.ApplyRows(ctx, projectID, domain.LocalManualDeviceID, rows, s.limits())
		if err != nil {
			return domain.LocalCommitResult{}, err
		}
		result.Accepted = applied + unchanged
	}
	batchID := trimmed(bundle.BatchID)
	if batchID == "" {
		batchID = domain.NewID()
	}
	s.record(ctx, auditRecord("local_sync.manual.commit", projectID, p.Username, map[string]any{
		"batch_id": batchID, "accepted": result.Accepted, "rejected": result.Rejected,
		"layers": layerNames(layers), "trace_mode": string(traceMode),
	}, nil))
	return result, nil
}

type wireRow struct {
	Layer         string          `json:"layer"`
	ID            string          `json:"id"`
	ContentHash   string          `json:"contentHash"`
	ContentDigest string          `json:"contentDigest"`
	UpdatedAt     json.Number     `json:"updatedAt"`
	Title         string          `json:"title"`
	Summary       string          `json:"summary"`
	Payload       json.RawMessage `json:"payload"`
}

func validBatchID(id string) bool {
	if len(id) < minBatchIDLen || len(id) > maxBatchIDLen {
		return false
	}
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '.', r == '_', r == '-':
		default:
			return false
		}
	}
	return true
}

func normalizeDeclaration(rawLayers []string, rawManifest map[string]int, rawMode string) ([]domain.LocalLayer, map[string]int, domain.LocalTraceMode, error) {
	if len(rawLayers) == 0 {
		return nil, nil, "", domain.Invalid("local_sync", "layers 不能为空")
	}
	layers := make([]domain.LocalLayer, 0, len(rawLayers))
	seen := make(map[domain.LocalLayer]struct{}, len(rawLayers))
	for _, raw := range rawLayers {
		layer, ok := domain.ParseLocalLayer(raw)
		if !ok {
			return nil, nil, "", domain.Invalid("local_sync", "未知 layer: "+raw)
		}
		if _, dup := seen[layer]; dup {
			return nil, nil, "", domain.Invalid("local_sync", "layers 不能重复")
		}
		seen[layer] = struct{}{}
		layers = append(layers, layer)
	}
	manifest := make(map[string]int, len(layers))
	for _, layer := range layers {
		count, ok := rawManifest[string(layer)]
		if !ok {
			manifest[string(layer)] = 0
			continue
		}
		if count < 0 {
			return nil, nil, "", domain.Invalid("local_sync", "manifest 行数不能为负")
		}
		if int64(count) > domain.LocalRowCountLimit {
			// 单层声明就超过项目总上限时直接拒绝，避免 manifestRows 求和回绕。
			return nil, nil, "", domain.Invalid("local_sync", "manifest 行数超过项目上限")
		}
		manifest[string(layer)] = count
	}
	for key := range rawManifest {
		if _, ok := seen[domain.LocalLayer(key)]; !ok {
			return nil, nil, "", domain.Invalid("local_sync", "manifest 含未声明的 layer: "+key)
		}
	}
	mode := domain.LocalTraceMode(strings.ToLower(trimmed(rawMode)))
	if mode == "" {
		// 隐私安全的默认值：不声明就是 summary，绝不默认上传原始 trace。
		mode = domain.LocalTraceModeSummary
	}
	if !mode.Valid() {
		return nil, nil, "", domain.Invalid("local_sync", "traceMode 只能是 summary 或 full")
	}
	return layers, manifest, mode, nil
}

func manifestHash(manifest map[string]int) string {
	keys := make([]string, 0, len(manifest))
	for key := range manifest {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	sum := sha256.New()
	for _, key := range keys {
		_, _ = fmt.Fprintf(sum, "%s=%d\n", key, manifest[key])
	}
	return hex.EncodeToString(sum.Sum(nil))
}

func normalizeContentHash(raw string) (string, bool) {
	hash := strings.ToLower(trimmed(raw))
	if len(hash) != len("sha256:")+64 || !strings.HasPrefix(hash, "sha256:") {
		return "", false
	}
	for _, r := range hash[len("sha256:"):] {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return "", false
		}
	}
	return hash, true
}

func parseEpochMillis(raw string) (int64, bool) {
	value, err := strconv.ParseInt(trimmed(raw), 10, 64)
	if err != nil || value < 1 || value > maxEpochMillis {
		return 0, false
	}
	return value, true
}

func decodePayload(raw json.RawMessage) (map[string]any, string) {
	body := bytes.TrimSpace(raw)
	if len(body) == 0 || bytes.Equal(body, []byte("null")) {
		return map[string]any{}, ""
	}
	if body[0] != '{' {
		return nil, "payload 必须是 JSON 对象"
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil {
		return nil, "payload 不是合法的 JSON 对象"
	}
	if payload == nil {
		payload = map[string]any{}
	}
	return payload, ""
}

func encodePayload(payload map[string]any, maxBytes int) ([]byte, int64, error) {
	var plain bytes.Buffer
	encoder := json.NewEncoder(&plain)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(payload); err != nil {
		return nil, 0, err
	}
	size := plain.Len()
	if size > maxBytes {
		return nil, int64(size), errRowPayloadTooLarge
	}
	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	if _, err := writer.Write(plain.Bytes()); err != nil {
		return nil, 0, err
	}
	if err := writer.Close(); err != nil {
		return nil, 0, err
	}
	return compressed.Bytes(), int64(size), nil
}

// readLine 读取一行，超过 max 时丢弃整行并标记 tooLong。
func readLine(reader *bufio.Reader, maxBytes int) ([]byte, bool, error) {
	var (
		buf     []byte
		tooLong bool
	)
	for {
		chunk, err := reader.ReadSlice('\n')
		if !tooLong {
			if len(buf)+len(chunk) > maxBytes {
				tooLong = true
				buf = nil
			} else {
				buf = append(buf, chunk...)
			}
		}
		switch {
		case err == nil:
			return bytes.TrimRight(buf, "\r\n"), tooLong, nil
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		case errors.Is(err, io.EOF):
			if tooLong {
				return nil, true, nil
			}
			if len(buf) == 0 {
				return nil, false, io.EOF
			}
			return bytes.TrimRight(buf, "\r\n"), false, nil
		default:
			return nil, false, err
		}
	}
}

// maxReader 在解压后的字节数超过上限时返回错误，防止压缩炸弹。
type maxReader struct {
	r   io.Reader
	n   int64
	max int64
}

func (m *maxReader) Read(p []byte) (int, error) {
	if m.n >= m.max {
		return 0, errDecompressedTooLarge
	}
	if int64(len(p)) > m.max-m.n {
		p = p[:m.max-m.n]
	}
	n, err := m.r.Read(p)
	m.n += int64(n)
	return n, err
}

func appendCapped(dst, src []domain.LocalRejectedReason) []domain.LocalRejectedReason {
	for _, item := range src {
		if len(dst) >= maxRejectedReasons {
			return dst
		}
		dst = append(dst, item)
	}
	return dst
}

func replayResult(batch domain.LocalSyncBatch) (domain.LocalCommitResult, error) {
	if strings.TrimSpace(batch.ResultJSON) == "" {
		return domain.LocalCommitResult{}, fmt.Errorf("localsync: committed batch %s has no result", batch.ID)
	}
	var result domain.LocalCommitResult
	if err := json.Unmarshal([]byte(batch.ResultJSON), &result); err != nil {
		return domain.LocalCommitResult{}, fmt.Errorf("localsync: decode batch result: %w", err)
	}
	return result, nil
}

func (s *Service) purgeBatches(ctx context.Context, now time.Time) {
	if _, err := s.store.PurgeBatchesBefore(ctx, now.Add(-s.opts.BatchTTL)); err != nil {
		s.log.WarnContext(ctx, "purge local batches failed", slog.String("error", err.Error()))
	}
}

func (s *Service) limits() domain.LocalQuota {
	return domain.LocalQuota{LimitBytes: s.opts.QuotaBytes, LimitRows: s.opts.QuotaRows}
}

func (s *Service) auditCommit(ctx context.Context, id DeviceIdentity, batch domain.LocalSyncBatch, result domain.LocalCommitResult) {
	input := map[string]any{
		"batch_id":   batch.ID,
		"device_id":  id.DeviceID,
		"accepted":   result.Accepted,
		"rejected":   result.Rejected,
		"layers":     layerNames(batch.Layers),
		"trace_mode": string(batch.TraceMode),
	}
	s.record(ctx, auditRecord("local_sync.batch.commit", id.TenantID, id.Username, input, nil))
	if result.Rejected > 0 {
		rejectedInput := map[string]any{
			"batch_id": batch.ID, "device_id": id.DeviceID,
			"rejected": result.Rejected, "reason_limit": len(result.RejectedReasons),
		}
		s.record(ctx, auditRecord("local_sync.batch.reject", id.TenantID, id.Username, rejectedInput, nil))
	}
}

func layerNames(layers []domain.LocalLayer) []string {
	out := make([]string, 0, len(layers))
	for _, layer := range layers {
		out = append(out, string(layer))
	}
	return out
}
