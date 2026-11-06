package app

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
)

// localSyncRow 构造一条合法的 JSONL wire row。payload 是 JSON 对象字面量。
func localSyncRow(layer, id, hashChar, title, payload string) string {
	return fmt.Sprintf(
		`{"layer":%q,"id":%q,"contentHash":"sha256:%s","contentDigest":"sha256:%s","updatedAt":1760000000000,"title":%q,"summary":"sum-%s","payload":%s}`,
		layer, id, strings.Repeat(hashChar, 64), strings.Repeat(hashChar, 64), title, id, payload,
	)
}

func gzipLines(t *testing.T, lines ...string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	for _, line := range lines {
		if _, err := io.WriteString(zw, line+"\n"); err != nil {
			t.Fatalf("gzip write: %v", err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("gzip close: %v", err)
	}
	return buf.Bytes()
}

// authorizeLocalDevice 走完设备码、浏览器授权与令牌轮询，返回带设备令牌的客户端。
func authorizeLocalDevice(t *testing.T, e *e2eEnv, browser *e2eClient) *e2eClient {
	t.Helper()
	const tenantID = "cube-a"
	anon := e.client()
	created := anon.post("/api/local-sync/device/code", `{"deviceLabel":"laptop","agentKind":"claude"}`)
	expect(t, "device code", created, http.StatusOK)
	deviceCode, userCode := created.str("deviceCode"), created.str("userCode")
	if deviceCode == "" || userCode == "" {
		t.Fatalf("device code response missing fields: %s", created.raw)
	}
	pending := anon.post("/api/local-sync/device/token", fmt.Sprintf(`{"deviceCode":%q}`, deviceCode))
	expect(t, "pending poll", pending, http.StatusAccepted)
	if pending.code != 20200 || pending.str("status") != "authorization_pending" {
		t.Fatalf("pending poll envelope = %s", pending.raw)
	}
	page := browser.get("/__device?code=" + url.QueryEscape(userCode))
	expect(t, "device page", page, http.StatusOK)
	if !strings.Contains(page.raw, "授权本地插件") {
		t.Fatalf("device page body = %s", page.raw)
	}
	approved := browser.post("/__device",
		fmt.Sprintf(`{"userCode":%q,"tenantId":%q,"label":"laptop","allow":true}`, userCode, tenantID))
	expect(t, "approve", approved, http.StatusOK)
	granted := anon.post("/api/local-sync/device/token", fmt.Sprintf(`{"deviceCode":%q}`, deviceCode))
	expect(t, "token poll", granted, http.StatusOK)
	token := granted.str("deviceToken")
	if !strings.HasPrefix(token, "memosls_") {
		t.Fatalf("device token = %q", token)
	}
	replay := anon.post("/api/local-sync/device/token", fmt.Sprintf(`{"deviceCode":%q}`, deviceCode))
	expect(t, "replayed poll", replay, http.StatusBadRequest)
	device := e.client()
	device.bearer = token
	return device
}

func uploadBatch(t *testing.T, device *e2eClient, batchID, body string) {
	t.Helper()
	created := device.post("/api/local-sync/batches", body)
	expect(t, "create batch "+batchID, created, http.StatusOK)
}

func putGzipChunk(t *testing.T, device *e2eClient, batchID string, gz []byte) result {
	t.Helper()
	return putGzipChunkSeq(t, device, batchID, 0, gz)
}

func putGzipChunkSeq(t *testing.T, device *e2eClient, batchID string, seq int, gz []byte) result {
	t.Helper()
	return device.do(http.MethodPut,
		fmt.Sprintf("/api/local-sync/batches/%s/chunks/%d", batchID, seq),
		"application/gzip", string(gz))
}

func localSyncRecords(t *testing.T, got result) []map[string]any {
	t.Helper()
	return localSyncItems(t, got, "records")
}

func localSyncDevices(t *testing.T, got result) []map[string]any {
	t.Helper()
	return localSyncItems(t, got, "devices")
}

func localSyncItems(t *testing.T, got result, key string) []map[string]any {
	t.Helper()
	data, ok := got.data.(map[string]any)
	if !ok {
		t.Fatalf("data is not an object: %s", got.raw)
	}
	raw, ok := data[key].([]any)
	if !ok {
		t.Fatalf("%s missing: %s", key, got.raw)
	}
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		m, ok := item.(map[string]any)
		if !ok {
			t.Fatalf("record is not an object: %s", got.raw)
		}
		out = append(out, m)
	}
	return out
}

func TestE2ELocalSyncDeviceFlowAndIngest(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	bob := e.loginAs("bob")
	device := authorizeLocalDevice(t, e, bob)

	state := device.get("/api/local-sync/state")
	expect(t, "state", state, http.StatusOK)
	if state.str("tenantId") != "cube-a" || state.str("deviceId") == "" {
		t.Fatalf("state = %s", state.raw)
	}
	quota, _ := state.obj()["quota"].(map[string]any)
	if limit, _ := quota["limitBytes"].(float64); limit <= 0 {
		t.Fatalf("quota = %s", state.raw)
	}

	batchID := "batch-0001"
	rows := []string{
		localSyncRow("policy", "p1", "a", "policy one", `{"text":"hello"}`),
		localSyncRow("policy", "p2", "b", "policy two", `{"note":"world"}`),
	}
	uploadBatch(t, device, batchID, fmt.Sprintf(
		`{"batchId":%q,"layers":["policy"],"manifest":{"policy":2},"traceMode":"summary"}`, batchID))
	if chunk := putGzipChunk(t, device, batchID, gzipLines(t, rows...)); chunk.status != http.StatusOK {
		t.Fatalf("put chunk: %s", chunk.raw)
	}
	commit := device.post("/api/local-sync/batches/"+batchID+"/commit", "")
	expect(t, "commit", commit, http.StatusOK)
	if accepted, _ := commit.obj()["accepted"].(float64); accepted != 2 {
		t.Fatalf("commit = %s", commit.raw)
	}
	replay := device.post("/api/local-sync/batches/"+batchID+"/commit", "")
	expect(t, "commit replay", replay, http.StatusOK)
	if accepted, _ := replay.obj()["accepted"].(float64); accepted != 2 {
		t.Fatalf("replayed commit = %s", replay.raw)
	}

	// 同一内容换一个批次再传：仍计入 accepted，但不会产生第二行。
	secondID := "batch-0002"
	uploadBatch(t, device, secondID, fmt.Sprintf(
		`{"batchId":%q,"layers":["policy"],"manifest":{"policy":1},"traceMode":"summary"}`, secondID))
	if chunk := putGzipChunk(t, device, secondID, gzipLines(t, rows[0])); chunk.status != http.StatusOK {
		t.Fatalf("put unchanged chunk: %s", chunk.raw)
	}
	unchanged := device.post("/api/local-sync/batches/"+secondID+"/commit", "")
	expect(t, "unchanged commit", unchanged, http.StatusOK)
	if accepted, _ := unchanged.obj()["accepted"].(float64); accepted != 1 {
		t.Fatalf("unchanged commit = %s", unchanged.raw)
	}

	list := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a")
	expect(t, "dashboard list", list, http.StatusOK)
	records := localSyncRecords(t, list)
	if total, _ := list.obj()["total"].(float64); total != 2 || len(records) != 2 {
		t.Fatalf("list total=%v records=%d body=%s", list.obj()["total"], len(records), list.raw)
	}
	for _, record := range records {
		if record["deviceLabel"] != "laptop" {
			t.Fatalf("record device label = %s", list.raw)
		}
	}
	if future := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a&startTime=2026-01-01"); future.obj()["total"] != float64(0) {
		t.Fatalf("startTime filter = %s", future.raw)
	}
	if past := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a&endTime=2026-01-01"); past.obj()["total"] != float64(2) {
		t.Fatalf("endTime filter = %s", past.raw)
	}
	badTime := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a&startTime=not-a-time")
	expect(t, "bad startTime", badTime, http.StatusBadRequest)

	summary := bob.get("/api/dashboard/local-sync/summary?projectId=cube-a")
	expect(t, "summary", summary, http.StatusOK)
	summaryBody := summary.obj()
	if got, _ := summaryBody["total"].(float64); got != 2 {
		t.Fatalf("summary total = %v body=%s", summaryBody["total"], summary.raw)
	}
	layers, _ := summaryBody["layers"].(map[string]any)
	if got, _ := layers["policy"].(float64); got != 2 {
		t.Fatalf("summary layers = %v body=%s", layers, summary.raw)
	}
	if summaryBody["oldestSyncedAt"] == "" || summaryBody["newestSyncedAt"] == "" {
		t.Fatalf("summary timestamps missing: %s", summary.raw)
	}
	if quota, _ := summaryBody["quota"].(map[string]any); quota == nil {
		t.Fatalf("summary quota missing: %s", summary.raw)
	} else if limit, _ := quota["limitBytes"].(float64); limit <= 0 {
		t.Fatalf("summary quota limit = %v body=%s", quota["limitBytes"], summary.raw)
	}
	memoryID, _ := records[0]["id"].(string)
	if memoryID == "" {
		t.Fatalf("memory id missing: %s", list.raw)
	}

	detail := bob.get("/api/dashboard/local-sync/memories/" + memoryID + "?projectId=cube-a")
	expect(t, "memory detail", detail, http.StatusOK)
	if payload, _ := detail.obj()["payload"].(map[string]any); len(payload) == 0 {
		t.Fatalf("detail payload = %s", detail.raw)
	}

	promotePath := "/api/dashboard/local-sync/memories/promote?projectId=cube-a"
	promoted := bob.do(http.MethodPost, promotePath, "application/json", fmt.Sprintf(`{"ids":[%q]}`, memoryID))
	expect(t, "promote", promoted, http.StatusOK)
	if queued, _ := promoted.obj()["queued"].([]any); len(queued) != 1 {
		t.Fatalf("promote = %s", promoted.raw)
	}
	againPromoted := bob.do(http.MethodPost, promotePath, "application/json", fmt.Sprintf(`{"ids":[%q]}`, memoryID))
	expect(t, "promote idempotent", againPromoted, http.StatusOK)
	if queued, _ := againPromoted.obj()["queued"].([]any); len(queued) != 1 {
		t.Fatalf("replayed promote = %s", againPromoted.raw)
	}

	deleted := bob.do(http.MethodDelete,
		"/api/dashboard/local-sync/memories/"+memoryID+"?projectId=cube-a", "", "")
	expect(t, "delete memory", deleted, http.StatusOK)
	after := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a")
	expect(t, "list after delete", after, http.StatusOK)
	if total, _ := after.obj()["total"].(float64); total != 1 {
		t.Fatalf("total after delete = %s", after.raw)
	}
	deletedSummary := bob.get("/api/dashboard/local-sync/summary?projectId=cube-a")
	expect(t, "summary after delete", deletedSummary, http.StatusOK)
	if got, _ := deletedSummary.obj()["deleted"].(float64); got != 1 {
		t.Fatalf("summary deleted = %v body=%s", deletedSummary.obj()["deleted"], deletedSummary.raw)
	}
	gone := bob.get("/api/dashboard/local-sync/memories/" + memoryID + "?projectId=cube-a")
	expect(t, "deleted detail", gone, http.StatusNotFound)

	// 删除只影响云端副本：本地再次上传同一内容不会复活该行，也不产生新行。
	sourceID := ""
	for _, record := range records {
		if record["id"] == memoryID {
			sourceID, _ = record["sourceId"].(string)
		}
	}
	hashChar := "a"
	if sourceID == "p2" {
		hashChar = "b"
	}
	reviveBatch := "batch-0003"
	uploadBatch(t, device, reviveBatch, fmt.Sprintf(
		`{"batchId":%q,"layers":["policy"],"manifest":{"policy":1},"traceMode":"summary"}`, reviveBatch))
	if chunk := putGzipChunk(t, device, reviveBatch,
		gzipLines(t, localSyncRow("policy", sourceID, hashChar, "revive", `{"text":"same hash"}`))); chunk.status != http.StatusOK {
		t.Fatalf("put revive chunk: %s", chunk.raw)
	}
	revive := device.post("/api/local-sync/batches/"+reviveBatch+"/commit", "")
	expect(t, "revive commit", revive, http.StatusOK)
	if accepted, _ := revive.obj()["accepted"].(float64); accepted != 1 {
		t.Fatalf("revive commit = %s", revive.raw)
	}
	stillGone := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a")
	expect(t, "list after revive", stillGone, http.StatusOK)
	if total, _ := stillGone.obj()["total"].(float64); total != 1 {
		t.Fatalf("deleted row revived: %s", stillGone.raw)
	}

	devices := bob.get("/api/dashboard/local-sync/devices?projectId=cube-a")
	expect(t, "devices", devices, http.StatusOK)
	deviceRecords := localSyncDevices(t, devices)
	if len(deviceRecords) != 1 {
		t.Fatalf("devices = %s", devices.raw)
	}
	if deviceRecords[0]["deviceId"] != state.str("deviceId") || deviceRecords[0]["label"] != "laptop" {
		t.Fatalf("device fields = %s", devices.raw)
	}
	publicID, _ := deviceRecords[0]["id"].(string)
	revoked := bob.do(http.MethodDelete, "/api/dashboard/local-sync/devices/"+publicID+"?projectId=cube-a", "", "")
	expect(t, "revoke device", revoked, http.StatusOK)
	afterRevoke := device.get("/api/local-sync/state")
	expect(t, "state after revoke", afterRevoke, http.StatusUnauthorized)

	// 跨项目：bob 是 cube-a editor，不是 cube-b 成员。
	crossProject := bob.get("/api/dashboard/local-sync/memories?projectId=cube-b")
	expect(t, "cross project", crossProject, http.StatusForbidden)
}

func TestE2ELocalSyncRejectsRawTraceAndBadRows(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	bob := e.loginAs("bob")
	device := authorizeLocalDevice(t, e, bob)

	rawBatch := "batch-trace1"
	uploadBatch(t, device, rawBatch, fmt.Sprintf(
		`{"batchId":%q,"layers":["trace"],"manifest":{"trace":1},"traceMode":"summary"}`, rawBatch))
	rawRow := localSyncRow("trace", "t-raw", "c", "raw trace", `{"summary":"ok","content":"RAW SECRET"}`)
	if chunk := putGzipChunk(t, device, rawBatch, gzipLines(t, rawRow)); chunk.status != http.StatusOK {
		t.Fatalf("put raw trace chunk: %s", chunk.raw)
	}
	rejected := device.post("/api/local-sync/batches/"+rawBatch+"/commit", "")
	expect(t, "raw trace commit", rejected, http.StatusOK)
	if accepted, _ := rejected.obj()["accepted"].(float64); accepted != 0 {
		t.Fatalf("raw trace accepted = %s", rejected.raw)
	}
	reasons, _ := rejected.obj()["rejectedReasons"].([]any)
	if len(reasons) != 1 {
		t.Fatalf("raw trace reasons = %s", rejected.raw)
	}
	if reason, _ := reasons[0].(map[string]any)["reason"].(string); reason == "" {
		t.Fatalf("raw trace reason missing = %s", rejected.raw)
	}

	okBatch := "batch-trace2"
	uploadBatch(t, device, okBatch, fmt.Sprintf(
		`{"batchId":%q,"layers":["trace"],"manifest":{"trace":1},"traceMode":"summary"}`, okBatch))
	okRow := localSyncRow("trace", "t-ok", "d", "ok trace",
		`{"summary":"ok","tags":["x"],"toolNames":["bash"],"toolCount":1,"ts":123}`)
	if chunk := putGzipChunk(t, device, okBatch, gzipLines(t, okRow)); chunk.status != http.StatusOK {
		t.Fatalf("put ok trace chunk: %s", chunk.raw)
	}
	acceptedTrace := device.post("/api/local-sync/batches/"+okBatch+"/commit", "")
	expect(t, "ok trace commit", acceptedTrace, http.StatusOK)
	if accepted, _ := acceptedTrace.obj()["accepted"].(float64); accepted != 1 {
		t.Fatalf("ok trace commit = %s", acceptedTrace.raw)
	}

	// 单行失败不整批失败：合法行照常入库，非法行只回报原因。
	partialBatch := "batch-part1"
	uploadBatch(t, device, partialBatch, fmt.Sprintf(
		`{"batchId":%q,"layers":["policy"],"manifest":{"policy":3},"traceMode":"summary"}`, partialBatch))
	partialRows := []string{
		localSyncRow("policy", "p-ok", "e", "partial ok", `{"text":"keep"}`),
		`{"layer":"policy","id":"p-bad","contentHash":"not-a-hash","updatedAt":1760000000000,"payload":{}}`,
		// contentDigest 是可选的，但给了就必须是同一编码；否则全局行级幂等和墓碑
		// 会静默退化，宁可拒绝这一行。
		fmt.Sprintf(`{"layer":"policy","id":"p-bad-digest","contentHash":"sha256:%s","contentDigest":"nope","updatedAt":1760000000000,"payload":{}}`,
			strings.Repeat("a", 64)),
	}
	if chunk := putGzipChunk(t, device, partialBatch, gzipLines(t, partialRows...)); chunk.status != http.StatusOK {
		t.Fatalf("put partial chunk: %s", chunk.raw)
	}
	partial := device.post("/api/local-sync/batches/"+partialBatch+"/commit", "")
	expect(t, "partial commit", partial, http.StatusOK)
	if accepted, _ := partial.obj()["accepted"].(float64); accepted != 1 {
		t.Fatalf("partial accepted = %s", partial.raw)
	}
	if count, _ := partial.obj()["rejected"].(float64); count != 2 {
		t.Fatalf("partial rejected = %s", partial.raw)
	}

	// 手工上传只接受控制台会话；管理 Token 明确拒绝。
	manualPath := "/api/local-sync/manual"
	manualRow := localSyncRow("policy", "s1", "f", "manual policy", `{"text":"local"}`)
	manual := bob.post(manualPath, fmt.Sprintf(
		`{"projectId":"cube-a","batchId":"manual-0001","layers":["policy"],"manifest":{"policy":1},"traceMode":"summary","rows":[%s]}`,
		manualRow))
	expect(t, "manual", manual, http.StatusOK)
	if accepted, _ := manual.obj()["accepted"].(float64); accepted != 1 {
		t.Fatalf("manual = %s", manual.raw)
	}
	adminManual := e.adminClient().post(manualPath, fmt.Sprintf(
		`{"projectId":"cube-a","layers":["policy"],"manifest":{"policy":1},"rows":[%s]}`, manualRow))
	expect(t, "manual with admin token", adminManual, http.StatusBadRequest)
}

func TestE2ELocalSyncLimits(t *testing.T) {
	t.Parallel()

	t.Run("chunk 413", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.MaxChunkBytes = 64
			cfg.LocalSync.MaxBatchBytes = 1 << 20
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-big1"
		uploadBatch(t, device, batchID, fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":1},"traceMode":"summary"}`, batchID))
		tooBig := putGzipChunk(t, device, batchID, bytes.Repeat([]byte("x"), 128))
		expect(t, "oversized chunk", tooBig, http.StatusRequestEntityTooLarge)
		if tooBig.code != 41300 {
			t.Fatalf("oversized chunk code = %d body=%s", tooBig.code, tooBig.raw)
		}
	})

	t.Run("manifest rows over quota 413 at create", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.QuotaRows = 1
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-quot1"
		over := device.post("/api/local-sync/batches", fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":2},"traceMode":"summary"}`, batchID))
		expect(t, "manifest over quota", over, http.StatusRequestEntityTooLarge)
		if over.code != 41300 {
			t.Fatalf("quota code = %d body=%s", over.code, over.raw)
		}
	})

	t.Run("quota across chunks is all-or-nothing", func(t *testing.T) {
		t.Parallel()
		// 行数多、内容重复：分块压缩后很小（能通过分块配额），但每行落库时的独立
		// gzip 有固定开销，commit 的字节配额会超，用来看跨块原子性。
		payload := `{"text":"quota-one"}`
		rowsPerChunk := 40
		chunks := make([][]byte, 2)
		for seq, prefix := range []string{"qa", "qb"} {
			lines := make([]string, 0, rowsPerChunk)
			for i := range rowsPerChunk {
				lines = append(lines, localSyncRow("policy", fmt.Sprintf("%s%d", prefix, i), "5", "quota row", payload))
			}
			chunks[seq] = gzipLines(t, lines...)
		}
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			// 分块总量放得下，落库后的 payload 总量放不下。
			cfg.LocalSync.QuotaBytes = int64(len(chunks[0])+len(chunks[1])) + 64
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-quot2"
		uploadBatch(t, device, batchID, fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":%d},"traceMode":"summary"}`, batchID, rowsPerChunk*2))
		for seq, gz := range chunks {
			if chunk := putGzipChunkSeq(t, device, batchID, seq, gz); chunk.status != http.StatusOK {
				t.Fatalf("put chunk %d: %s", seq, chunk.raw)
			}
		}
		over := device.post("/api/local-sync/batches/"+batchID+"/commit", "")
		expect(t, "cross-chunk quota", over, http.StatusRequestEntityTooLarge)
		if over.code != 41300 {
			t.Fatalf("cross-chunk quota code = %d body=%s", over.code, over.raw)
		}
		// The first chunk must not survive a quota failure in a later chunk.
		list := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a")
		expect(t, "list after quota failure", list, http.StatusOK)
		if total, _ := list.obj()["total"].(float64); total != 0 {
			t.Fatalf("rows persisted after quota failure = %v, raw=%s", total, list.raw)
		}
	})

	t.Run("write 429", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.RatePerMinute = 1
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-rate1"
		uploadBatch(t, device, batchID, fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":1},"traceMode":"summary"}`, batchID))
		limited := putGzipChunk(t, device, batchID, gzipLines(t, localSyncRow("policy", "r1", "1", "rate", `{}`)))
		expect(t, "rate limited", limited, http.StatusTooManyRequests)
		if limited.code != 42900 || limited.header.Get("Retry-After") == "" {
			t.Fatalf("rate limit response code=%d header=%v body=%s", limited.code, limited.header, limited.raw)
		}
	})

	t.Run("disabled 503", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = false
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		devices := bob.get("/api/dashboard/local-sync/devices?projectId=cube-a")
		expect(t, "disabled dashboard", devices, http.StatusServiceUnavailable)
		if devices.code != 50300 {
			t.Fatalf("disabled dashboard code = %d body=%s", devices.code, devices.raw)
		}
		state := e.client().get("/api/local-sync/state")
		expect(t, "disabled state", state, http.StatusServiceUnavailable)
		page := bob.get("/__device?code=ABCD-EFGH")
		expect(t, "disabled device page", page, http.StatusServiceUnavailable)
	})
}

// TestE2ELocalSyncPromoteRunsThroughWorker 验证转存经 jobs 队列调用上游 add_message，
// 成功后回写状态，且重复转存不会再次写上游。
func TestE2ELocalSyncPromoteRunsThroughWorker(t *testing.T) {
	t.Parallel()
	var adds atomic.Int32
	var lastPayload atomic.Value
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/product/contract":
			_, _ = io.WriteString(w, goodContract)
		case "/product/add":
			adds.Add(1)
			raw, _ := io.ReadAll(r.Body)
			var payload map[string]any
			_ = json.Unmarshal(raw, &payload)
			lastPayload.Store(payload)
			_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"status":"success"}}`)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(upstream.Close)

	e := newE2EConfig(t, func(cfg *config.Config) {
		cfg.Upstream.BaseURL = upstream.URL
		cfg.Worker.PollInterval = 10 * time.Millisecond
		cfg.LocalSync.Enabled = true
	})
	seedWorld(t, e)
	bob := e.loginAs("bob")
	device := authorizeLocalDevice(t, e, bob)

	ctx, cancel := context.WithCancel(context.Background())
	done, stop := e.app.startWorker(ctx)
	t.Cleanup(func() {
		stop()
		cancel()
		select {
		case err := <-done:
			_ = err
		case <-time.After(5 * time.Second):
		}
	})

	batchID := "batch-prom1"
	uploadBatch(t, device, batchID, fmt.Sprintf(
		`{"batchId":%q,"layers":["policy"],"manifest":{"policy":1},"traceMode":"summary"}`, batchID))
	if chunk := putGzipChunk(t, device, batchID,
		gzipLines(t, localSyncRow("policy", "p-prom", "2", "promote me", `{"text":"hello promote"}`))); chunk.status != http.StatusOK {
		t.Fatalf("put chunk: %s", chunk.raw)
	}
	commit := device.post("/api/local-sync/batches/"+batchID+"/commit", "")
	expect(t, "commit", commit, http.StatusOK)

	list := bob.get("/api/dashboard/local-sync/memories?projectId=cube-a")
	expect(t, "list", list, http.StatusOK)
	records := localSyncRecords(t, list)
	if len(records) != 1 {
		t.Fatalf("list = %s", list.raw)
	}
	memoryID, _ := records[0]["id"].(string)
	promoted := bob.do(http.MethodPost, "/api/dashboard/local-sync/memories/promote?projectId=cube-a",
		"application/json", fmt.Sprintf(`{"ids":[%q]}`, memoryID))
	expect(t, "promote", promoted, http.StatusOK)

	deadline := time.Now().Add(5 * time.Second)
	var detail result
	for time.Now().Before(deadline) {
		detail = bob.get("/api/dashboard/local-sync/memories/" + memoryID + "?projectId=cube-a")
		expect(t, "detail", detail, http.StatusOK)
		if detail.str("promoteStatus") == "succeeded" {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if detail.str("promoteStatus") != "succeeded" {
		t.Fatalf("promote did not finish: %s", detail.raw)
	}
	// promoted 由转存状态决定，不能依赖 promotedMemoryId：上游 add 不返回记忆 ID，
	// 这个字段成功时也就是空的。
	if promoted, _ := detail.obj()["promoted"].(bool); !promoted {
		t.Fatalf("promoted flag should be true after success: %s", detail.raw)
	}
	if detail.str("promotedMemoryId") != "" {
		t.Fatalf("promotedMemoryId must stay empty until upstream returns a real id: %s", detail.raw)
	}
	if adds.Load() != 1 {
		t.Fatalf("upstream add calls = %d", adds.Load())
	}
	payload, _ := lastPayload.Load().(map[string]any)
	if payload == nil {
		t.Fatal("upstream add payload missing")
	}
	messages, _ := json.Marshal(payload["messages"])
	if !bytes.Contains(messages, []byte("hello promote")) {
		t.Fatalf("upstream messages = %s", messages)
	}

	// 已成功的行再次转存只回 queued，不产生第二次上游写入。
	again := bob.do(http.MethodPost, "/api/dashboard/local-sync/memories/promote?projectId=cube-a",
		"application/json", fmt.Sprintf(`{"ids":[%q]}`, memoryID))
	expect(t, "promote again", again, http.StatusOK)
	if queued, _ := again.obj()["queued"].([]any); len(queued) != 1 {
		t.Fatalf("promote again = %s", again.raw)
	}
	time.Sleep(100 * time.Millisecond)
	if adds.Load() != 1 {
		t.Fatalf("upstream add calls after replay = %d", adds.Load())
	}
}

// TestE2ELocalSyncPlatformAdminCanAuthorize 回归：平台管理员的 Principal.Roles
// 按设计是空的（隐含权限不落表）。授权判权若只看这个映射，既是平台管理员、
// 又是项目 owner 的账号会被误判成无上传权限（403）。
func TestE2ELocalSyncPlatformAdminCanAuthorize(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	// root 是种子账号，创建时即 PlatformAdmin；它的 Principal.Roles 恒为空。
	expect(t, "member root",
		e.adminClient().post("/admin/tenants/cube-a/members", `{"username":"root","role":"owner"}`),
		http.StatusOK)

	root := e.client()
	expect(t, "login root", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	device := authorizeLocalDevice(t, e, root)
	expect(t, "platform admin device state", device.get("/api/local-sync/state"), http.StatusOK)
}

// TestE2EDeviceApproveAcceptsBrowserNullOrigin 回归：Chrome 对同源 HTML 表单提交
// （导航型 POST）会发 Origin: null。旧实现把它当非法来源，浏览器里授权必然失败；
// 这里用真实表单编码复现，并确认真正的跨站提交仍被拒。
func TestE2EDeviceApproveAcceptsBrowserNullOrigin(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	bob := e.loginAs("bob")

	anon := e.client()
	created := anon.post("/api/local-sync/device/code", `{"deviceLabel":"chrome","agentKind":"claude"}`)
	expect(t, "device code", created, http.StatusOK)
	deviceCode, userCode := created.str("deviceCode"), created.str("userCode")

	form := url.Values{
		"userCode": {userCode},
		"tenantId": {"cube-a"},
		"label":    {"chrome"},
		"allow":    {"true"},
	}
	bob.headers = map[string]string{"Origin": "null", "Sec-Fetch-Site": "same-origin"}
	approved := bob.do(http.MethodPost, "/__device", "application/x-www-form-urlencoded", form.Encode())
	expect(t, "approve with null origin", approved, http.StatusOK)
	if strings.Contains(approved.raw, "拒绝跨站提交") {
		t.Fatalf("browser form post was rejected as cross-site: %s", approved.raw)
	}

	granted := anon.post("/api/local-sync/device/token", fmt.Sprintf(`{"deviceCode":%q}`, deviceCode))
	expect(t, "token poll after null-origin approve", granted, http.StatusOK)

	second := anon.post("/api/local-sync/device/code", `{"deviceLabel":"evil","agentKind":"claude"}`)
	expect(t, "second device code", second, http.StatusOK)
	crossForm := url.Values{
		"userCode": {second.str("userCode")},
		"tenantId": {"cube-a"},
		"label":    {"evil"},
		"allow":    {"true"},
	}
	bob.headers = map[string]string{"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
	denied := bob.do(http.MethodPost, "/__device", "application/x-www-form-urlencoded", crossForm.Encode())
	expect(t, "cross-site approve", denied, http.StatusForbidden)
	if !strings.Contains(denied.raw, "拒绝跨站提交") {
		t.Fatalf("cross-site body = %s", denied.raw)
	}
}

// TestE2ELocalSyncResourceGuards 覆盖独立审查发现的三个资源缺口：
// 未提交分块占配额、manifest 行数占配额、整批解压总量封顶，以及 41300 只用于配额。
func TestE2ELocalSyncResourceGuards(t *testing.T) {
	t.Parallel()

	t.Run("pending chunks count against byte quota", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.QuotaBytes = 1024
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-pend1"
		uploadBatch(t, device, batchID, fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":2},"traceMode":"summary"}`, batchID))
		first := putGzipChunkSeq(t, device, batchID, 0, bytes.Repeat([]byte("a"), 700))
		expect(t, "first pending chunk", first, http.StatusOK)
		second := putGzipChunkSeq(t, device, batchID, 1, bytes.Repeat([]byte("b"), 700))
		expect(t, "pending chunk over quota", second, http.StatusRequestEntityTooLarge)
		if second.code != 41300 {
			t.Fatalf("pending quota code = %d body=%s", second.code, second.raw)
		}
	})

	t.Run("manifest rows count against row quota", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.QuotaRows = 1
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-rows1"
		over := device.post("/api/local-sync/batches", fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":2},"traceMode":"summary"}`, batchID))
		expect(t, "manifest over row quota", over, http.StatusRequestEntityTooLarge)
		if over.code != 41300 {
			t.Fatalf("manifest quota code = %d body=%s", over.code, over.raw)
		}
	})

	t.Run("whole batch decompressed size is capped", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.MaxChunkBytes = 4096
			cfg.LocalSync.MaxBatchBytes = 8192
			cfg.LocalSync.MaxBatchDecompressed = 1024
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		batchID := "batch-deco1"
		uploadBatch(t, device, batchID, fmt.Sprintf(
			`{"batchId":%q,"layers":["policy"],"manifest":{"policy":2},"traceMode":"summary"}`, batchID))
		big := strings.Repeat("a", 600)
		chunk := gzipLines(t,
			localSyncRow("policy", "d1", "5", "deco one", `{"text":"`+big+`"}`),
			localSyncRow("policy", "d2", "6", "deco two", `{"text":"`+big+`"}`),
		)
		if len(chunk) >= 4096 {
			t.Fatalf("compressed chunk too large for test: %d", len(chunk))
		}
		if put := putGzipChunk(t, device, batchID, chunk); put.status != http.StatusOK {
			t.Fatalf("put decomposable chunk: %s", put.raw)
		}
		over := device.post("/api/local-sync/batches/"+batchID+"/commit", "")
		expect(t, "decompressed cap", over, http.StatusBadRequest)
		if over.code != 40000 {
			t.Fatalf("decompressed cap code = %d body=%s", over.code, over.raw)
		}
	})

	t.Run("http body too large keeps 40000", func(t *testing.T) {
		t.Parallel()
		e := newE2EConfig(t, func(cfg *config.Config) {
			cfg.HTTP.MaxBodyBytes = 512
			cfg.LocalSync.Enabled = true
			cfg.LocalSync.MaxChunkBytes = 256
			cfg.LocalSync.MaxBatchBytes = 1024
			cfg.LocalSync.MaxBatchDecompressed = 2048
		})
		seedWorld(t, e)
		bob := e.loginAs("bob")
		device := authorizeLocalDevice(t, e, bob)
		big := strings.Repeat("x", 2000)
		over := device.post("/api/local-sync/batches", fmt.Sprintf(
			`{"batchId":"batch-body1","layers":["policy"],"manifest":{"policy":1},"traceMode":"summary","note":%q}`, big))
		expect(t, "body too large", over, http.StatusRequestEntityTooLarge)
		if over.code != 40000 {
			t.Fatalf("body too large code = %d body=%s", over.code, over.raw)
		}
	})
}

// TestE2ELocalSyncPendingQuotaVisible 未提交批次的分块与 manifest 行数占配额，
// 对外也必须可见：否则界面显示还有空间，上传却直接 413。
func TestE2ELocalSyncPendingQuotaVisible(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	bob := e.loginAs("bob")
	device := authorizeLocalDevice(t, e, bob)

	const batchID = "batch-pending-0001"
	uploadBatch(t, device, batchID,
		`{"batchId":"`+batchID+`","layers":["trace"],"manifest":{"trace":2},"traceMode":"summary"}`)

	quota := func(got result) map[string]any {
		t.Helper()
		m, _ := got.obj()["quota"].(map[string]any)
		if m == nil {
			t.Fatalf("quota missing: %s", got.raw)
		}
		return m
	}
	num := func(m map[string]any, key string) float64 {
		t.Helper()
		v, _ := m[key].(float64)
		return v
	}

	state := device.get("/api/local-sync/state")
	expect(t, "state with pending batch", state, http.StatusOK)
	if got := num(quota(state), "pendingRows"); got != 2 {
		t.Fatalf("state pendingRows = %v body=%s", got, state.raw)
	}

	gz := gzipLines(t,
		localSyncRow("trace", "t1", "a", "trace one", `{"summary":"s1","tags":[],"toolCount":0,"ts":1760000000000}`),
		localSyncRow("trace", "t2", "b", "trace two", `{"summary":"s2","tags":[],"toolCount":0,"ts":1760000000000}`),
	)
	expect(t, "put chunk", putGzipChunk(t, device, batchID, gz), http.StatusOK)

	state = device.get("/api/local-sync/state")
	if got := num(quota(state), "pendingBytes"); got <= 0 {
		t.Fatalf("state pendingBytes = %v body=%s", got, state.raw)
	}
	summary := bob.get("/api/dashboard/local-sync/summary?projectId=cube-a")
	expect(t, "summary with pending batch", summary, http.StatusOK)
	if got := num(quota(summary), "pendingRows"); got != 2 {
		t.Fatalf("summary pendingRows = %v body=%s", got, summary.raw)
	}
	if got := num(quota(summary), "pendingBytes"); got <= 0 {
		t.Fatalf("summary pendingBytes = %v body=%s", got, summary.raw)
	}

	committed := device.post("/api/local-sync/batches/"+batchID+"/commit", `{}`)
	expect(t, "commit", committed, http.StatusOK)
	state = device.get("/api/local-sync/state")
	if got := num(quota(state), "pendingBytes"); got != 0 {
		t.Fatalf("pendingBytes after commit = %v body=%s", got, state.raw)
	}
	if got := num(quota(state), "pendingRows"); got != 0 {
		t.Fatalf("pendingRows after commit = %v body=%s", got, state.raw)
	}
	if got := num(quota(state), "usedRows"); got != 2 {
		t.Fatalf("usedRows after commit = %v body=%s", got, state.raw)
	}
}

// TestE2ELocalSyncCreateBatchRejectsReplayBeforeQuota 客户端在响应丢失后重发同一
// batchId，应得到 409（幂等冲突），而不是因为 pending 已计入配额而误报 413。
func TestE2ELocalSyncCreateBatchRejectsReplayBeforeQuota(t *testing.T) {
	t.Parallel()
	e := newE2EConfig(t, func(cfg *config.Config) {
		cfg.LocalSync.QuotaRows = 2
	})
	seedWorld(t, e)
	bob := e.loginAs("bob")
	device := authorizeLocalDevice(t, e, bob)

	const body = `{"batchId":"batch-replay-0001","layers":["trace"],"manifest":{"trace":2},"traceMode":"summary"}`
	create := device.post("/api/local-sync/batches", body)
	expect(t, "first create", create, http.StatusOK)
	replay := device.post("/api/local-sync/batches", body)
	expect(t, "replayed create", replay, http.StatusConflict)
	if replay.code != 40900 {
		t.Fatalf("replayed create code = %d body=%s", replay.code, replay.raw)
	}

	overflow := device.post("/api/local-sync/batches",
		`{"batchId":"batch-overflow-0001","layers":["trace"],"manifest":{"trace":9223372036854775807},"traceMode":"summary"}`)
	expect(t, "manifest overflow", overflow, http.StatusBadRequest)
}
