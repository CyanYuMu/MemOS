package memory

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type fakeUpstream struct {
	add      domain.AddPayload
	search   domain.SearchQuery
	profile  domain.ProfileQuery
	feedback domain.FeedbackRequest

	searchResult   domain.SearchResult
	feedbackResult domain.FeedbackResult
	owned          map[string]bool
	owners         map[string]string
	ownersErr      error
	deleted        []string
	deletedTenant  string
	err            error
	addCalls       int
}

func (f *fakeUpstream) Add(_ context.Context, req domain.AddPayload) (domain.AddResult, error) {
	f.add = req
	f.addCalls++
	return domain.AddResult{Status: "success", Message: "Memory added successfully"}, f.err
}

func (f *fakeUpstream) Search(_ context.Context, q domain.SearchQuery) (domain.SearchResult, error) {
	f.search = q
	return f.searchResult, f.err
}

func (f *fakeUpstream) Profile(_ context.Context, q domain.ProfileQuery) (domain.ProfileResult, error) {
	f.profile = q
	return domain.ProfileResult{Current: q.Page, Size: q.PageSize}, f.err
}

func (f *fakeUpstream) OwnedMemory(_ context.Context, _, _, id string) (domain.Memory, bool, error) {
	return domain.Memory{ID: id}, f.owned[id], f.err
}

func (f *fakeUpstream) OwnedMemories(_ context.Context, _, _ string, ids []string) (map[string]bool, error) {
	out := make(map[string]bool, len(ids))
	for _, id := range ids {
		if f.owned[id] {
			out[id] = true
		}
	}
	return out, f.err
}

func (f *fakeUpstream) Owners(_ context.Context, ids []string) (map[string]string, error) {
	if f.ownersErr != nil {
		return nil, f.ownersErr
	}
	out := make(map[string]string, len(ids))
	for _, id := range ids {
		if owner, ok := f.owners[id]; ok {
			out[id] = owner
		}
	}
	return out, nil
}

func (f *fakeUpstream) Delete(_ context.Context, tenantID string, ids []string) error {
	if f.err != nil {
		return f.err
	}
	f.deletedTenant = tenantID
	f.deleted = append(f.deleted, ids...)
	return nil
}

func (f *fakeUpstream) Feedback(_ context.Context, req domain.FeedbackRequest) (domain.FeedbackResult, error) {
	f.feedback = req
	return f.feedbackResult, f.err
}

type fakeJobs struct {
	job   domain.Job
	err   error
	count int
}

func (f *fakeJobs) EnqueueJob(_ context.Context, j domain.Job) error {
	f.job = j
	f.count++
	return f.err
}

type fakeIdem struct {
	records map[string]domain.IdempotencyRecord
}

func newFakeIdem() *fakeIdem {
	return &fakeIdem{records: map[string]domain.IdempotencyRecord{}}
}

func (f *fakeIdem) ClaimIdempotency(_ context.Context, rec domain.IdempotencyRecord) (domain.IdempotencyRecord, bool, error) {
	if existing, ok := f.records[rec.TenantID+"|"+rec.Key]; ok {
		return existing, false, nil
	}
	f.records[rec.TenantID+"|"+rec.Key] = rec
	return rec, true, nil
}

func (f *fakeIdem) CompleteIdempotency(_ context.Context, p domain.IdempotencyOutcome) error {
	f.set(p.TenantID, p.Key, domain.IdempotencySucceeded, p.Response)
	return nil
}

func (f *fakeIdem) AcceptIdempotency(_ context.Context, p domain.IdempotencyOutcome) error {
	f.set(p.TenantID, p.Key, domain.IdempotencyAccepted, p.Response)
	return nil
}

func (f *fakeIdem) SucceedIdempotency(_ context.Context, tenantID, key string, _ time.Time) error {
	rec := f.records[tenantID+"|"+key]
	rec.State = domain.IdempotencySucceeded
	f.records[tenantID+"|"+key] = rec
	return nil
}

func (f *fakeIdem) ReleaseIdempotency(_ context.Context, tenantID, key string) error {
	delete(f.records, tenantID+"|"+key)
	return nil
}

// setStateAll 把所有记录置为指定状态（保留已缓存的 Response），用于模拟 worker 终态收敛。
func (f *fakeIdem) setStateAll(state string) {
	for key := range f.records {
		rec := f.records[key]
		rec.State = state
		f.records[key] = rec
	}
}

func (f *fakeIdem) set(tenantID, key, state string, response []byte) {
	rec := f.records[tenantID+"|"+key]
	rec.State = state
	rec.Response = response
	f.records[tenantID+"|"+key] = rec
}

func caller() domain.Caller {
	return domain.Caller{TenantID: "cube-a", UserID: "alice", TokenID: "tok"}
}

func TestAddNormalizesAndBindsCaller(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{}, nil)

	raw := json.RawMessage(`[{"role":"user","content":"帮我记住 偏好"},{"role":"assistant","content":"好","tool_calls":[{"id":"c1","type":"function","function":{"name":"search_memory","arguments":"{}"}}]}]`)
	result, err := svc.Add(context.Background(), caller(), domain.AddRequest{
		Messages:     raw,
		Tags:         []string{" pref ", ""},
		FirstMessage: "你好",
		TenantID:     "attacker",
		UserID:       "mallory",
	})
	if err != nil {
		t.Fatalf("Add: %v", err)
	}
	if result.Status != "success" {
		t.Fatalf("result = %+v", result)
	}
	if up.add.TenantID != "cube-a" || up.add.UserID != "alice" {
		t.Fatalf("caller not enforced: %+v", up.add)
	}
	if up.add.SessionID != ResolveSessionID("", "你好") {
		t.Fatalf("session = %q", up.add.SessionID)
	}
	if len(up.add.Messages) != 2 || up.add.Messages[1].ToolCalls[0].Function.Name != "search_memory" {
		t.Fatalf("messages = %+v", up.add.Messages)
	}
	if len(up.add.Tags) != 1 || up.add.Tags[0] != "pref" {
		t.Fatalf("tags = %+v", up.add.Tags)
	}
}

func TestAddAcceptsJSONStringMessages(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{}, nil)
	// 数组里装的是被二次编码的 JSON 字符串。
	raw := json.RawMessage(`["[{\"role\":\"user\",\"content\":\"hi\"}]"]`)
	if _, err := svc.Add(context.Background(), caller(), domain.AddRequest{Messages: raw}); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if len(up.add.Messages) != 1 || up.add.Messages[0].Content != "hi" {
		t.Fatalf("messages = %+v", up.add.Messages)
	}
}

func TestAddAcceptsTopLevelJSONStringMessages(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{}, nil)
	// 整个 messages 是 JSON 字符串（MCP arguments 里很常见）。
	encoded, err := json.Marshal(`[{"role":"user","content":"hi"}]`)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if _, err := svc.Add(context.Background(), caller(), domain.AddRequest{Messages: encoded}); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if len(up.add.Messages) != 1 || up.add.Messages[0].Content != "hi" {
		t.Fatalf("messages = %+v", up.add.Messages)
	}
}

func TestAddInvalidMessages(t *testing.T) {
	t.Parallel()

	svc := New(&fakeUpstream{}, nil, Options{}, nil)
	for _, raw := range []string{``, `[]`, `{"role":"user"}`, `not json`} {
		if _, err := svc.Add(context.Background(), caller(), domain.AddRequest{Messages: json.RawMessage(raw)}); !errors.Is(err, domain.ErrInvalidInput) {
			t.Errorf("raw %q err = %v", raw, err)
		}
	}
}

func TestAddIdempotentReplaysFirstResult(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{Idem: newFakeIdem()}, nil)
	req := domain.AddRequest{
		Messages:       json.RawMessage(`[{"role":"user","content":"hi"}]`),
		IdempotencyKey: "req-1",
	}
	first, err := svc.Add(context.Background(), caller(), req)
	if err != nil {
		t.Fatalf("first Add: %v", err)
	}
	second, err := svc.Add(context.Background(), caller(), req)
	if err != nil {
		t.Fatalf("second Add: %v", err)
	}
	if up.addCalls != 1 {
		t.Fatalf("upstream calls=%d want 1", up.addCalls)
	}
	if second.Status != first.Status || second.Message != first.Message || second.Deferred != first.Deferred {
		t.Fatalf("replay = %+v want %+v", second, first)
	}
}

// 幂等重放必须带回首次请求实际使用的 call_id：transport 用它覆盖重放请求行的
// call_id，重放请求才能对上原任务的终态行。
func TestAddIdempotentReplaySharesCallID(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name   string
		finish string // 非空时模拟 worker 把记录收敛到该状态
	}{
		{name: "accepted 重放"},
		{name: "succeeded 重放", finish: domain.IdempotencySucceeded},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			idem := newFakeIdem()
			jobs := &fakeJobs{}
			svc := New(&fakeUpstream{}, jobs, Options{AddAsync: true, Idem: idem}, nil)
			req := domain.AddRequest{
				Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`),
				Async:    true,
				CallID:   "call-first",
			}
			first, err := svc.Add(context.Background(), caller(), req)
			if err != nil {
				t.Fatalf("first Add: %v", err)
			}
			if first.CallID != req.CallID {
				t.Fatalf("首次 CallID=%q want %q", first.CallID, req.CallID)
			}
			if tc.finish != "" {
				idem.setStateAll(tc.finish)
			}
			// 重放请求带不同的 call_id，返回的仍是首次的。
			req.CallID = "call-replay"
			second, err := svc.Add(context.Background(), caller(), req)
			if err != nil {
				t.Fatalf("replay Add: %v", err)
			}
			if second.CallID != first.CallID {
				t.Fatalf("重放 CallID=%q want %q", second.CallID, first.CallID)
			}
			if jobs.count != 1 {
				t.Fatalf("重放不应重复入队，enqueued=%d", jobs.count)
			}
		})
	}
}

// 不同内容不会被同一记录合并，各自生成自己的 job ID。
func TestAddIdempotentDifferentContentGetsNewCallID(t *testing.T) {
	t.Parallel()

	jobs := &fakeJobs{}
	svc := New(&fakeUpstream{}, jobs, Options{AddAsync: true, Idem: newFakeIdem()}, nil)
	first, err := svc.Add(context.Background(), caller(), domain.AddRequest{
		Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`),
		Async:    true,
		CallID:   "call-first",
	})
	if err != nil {
		t.Fatalf("first Add: %v", err)
	}
	second, err := svc.Add(context.Background(), caller(), domain.AddRequest{
		Messages: json.RawMessage(`[{"role":"user","content":"different"}]`),
		Async:    true,
	})
	if err != nil {
		t.Fatalf("second Add: %v", err)
	}
	if second.CallID == "" || second.CallID == first.CallID {
		t.Fatalf("不同内容应生成新的 call_id: first=%q second=%q", first.CallID, second.CallID)
	}
	if jobs.count != 2 {
		t.Fatalf("不同内容应各自入队，enqueued=%d", jobs.count)
	}
}

func TestAddIdempotentRejectsDifferentPayload(t *testing.T) {
	t.Parallel()

	svc := New(&fakeUpstream{}, nil, Options{Idem: newFakeIdem()}, nil)
	base := domain.AddRequest{
		Messages:       json.RawMessage(`[{"role":"user","content":"hi"}]`),
		IdempotencyKey: "req-1",
	}
	if _, err := svc.Add(context.Background(), caller(), base); err != nil {
		t.Fatalf("first Add: %v", err)
	}
	base.Messages = json.RawMessage(`[{"role":"user","content":"different"}]`)
	if _, err := svc.Add(context.Background(), caller(), base); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("different payload err=%v want conflict", err)
	}
}

func TestAddIdempotentReleasesOnFailure(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{err: domain.Wrap(domain.ErrUpstreamUnavailable, "add", "上游不可用", nil)}
	idem := newFakeIdem()
	svc := New(up, nil, Options{Idem: idem}, nil)
	req := domain.AddRequest{
		Messages:       json.RawMessage(`[{"role":"user","content":"hi"}]`),
		IdempotencyKey: "req-1",
	}
	if _, err := svc.Add(context.Background(), caller(), req); err == nil {
		t.Fatal("want upstream error")
	}
	if len(idem.records) != 0 {
		t.Fatalf("in-progress key should be released: %+v", idem.records)
	}
	up.err = nil
	if _, err := svc.Add(context.Background(), caller(), req); err != nil {
		t.Fatalf("retry after failure: %v", err)
	}
}

func TestAddDedupesIdenticalPayloadWithoutKey(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{Idem: newFakeIdem()}, nil)
	req := domain.AddRequest{Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`)}
	first, err := svc.Add(context.Background(), caller(), req)
	if err != nil {
		t.Fatalf("first Add: %v", err)
	}
	second, err := svc.Add(context.Background(), caller(), req)
	if err != nil {
		t.Fatalf("second Add: %v", err)
	}
	if up.addCalls != 1 {
		t.Fatalf("upstream calls=%d want 1", up.addCalls)
	}
	if second.Status != first.Status || second.Message != first.Message || second.Deferred != first.Deferred {
		t.Fatalf("replay = %+v want %+v", second, first)
	}
}

func TestAddDifferentContentIsNotDeduped(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{Idem: newFakeIdem()}, nil)
	for _, content := range []string{"hi", "different"} {
		if _, err := svc.Add(context.Background(), caller(), domain.AddRequest{
			Messages: json.RawMessage(`[{"role":"user","content":"` + content + `"}]`),
		}); err != nil {
			t.Fatalf("Add %q: %v", content, err)
		}
	}
	if up.addCalls != 2 {
		t.Fatalf("upstream calls=%d want 2", up.addCalls)
	}
}

func TestAddSyncAndAsyncDoNotShareRecord(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	jobs := &fakeJobs{}
	svc := New(up, jobs, Options{AddAsync: true, Idem: newFakeIdem()}, nil)
	req := domain.AddRequest{Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`)}
	if _, err := svc.Add(context.Background(), caller(), req); err != nil {
		t.Fatalf("sync Add: %v", err)
	}
	req.Async = true
	if _, err := svc.Add(context.Background(), caller(), req); err != nil {
		t.Fatalf("async Add: %v", err)
	}
	if up.addCalls != 1 || jobs.count != 1 {
		t.Fatalf("sync calls=%d async enqueues=%d want 1/1", up.addCalls, jobs.count)
	}
}

func TestAddAsyncDedupesAtEnqueue(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	jobs := &fakeJobs{}
	idem := newFakeIdem()
	svc := New(up, jobs, Options{AddAsync: true, Idem: idem}, nil)
	req := domain.AddRequest{Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`), Async: true}

	first, err := svc.Add(context.Background(), caller(), req)
	if err != nil || !first.Deferred {
		t.Fatalf("first Add = %+v, %v", first, err)
	}
	second, err := svc.Add(context.Background(), caller(), req)
	if err != nil || !second.Deferred {
		t.Fatalf("second Add = %+v, %v", second, err)
	}
	if jobs.count != 1 {
		t.Fatalf("enqueued=%d want 1", jobs.count)
	}
	if up.addCalls != 0 {
		t.Fatalf("async add should not call upstream, calls=%d", up.addCalls)
	}
	for _, rec := range idem.records {
		if rec.State != domain.IdempotencyAccepted || len(rec.Response) == 0 {
			t.Fatalf("record = %+v want accepted with response", rec)
		}
	}

	// worker 终态成功后记录收敛，重发仍然只重放。
	svc.ReportJobOutcome(context.Background(), jobs.job.Payload, nil)
	if _, err := svc.Add(context.Background(), caller(), req); err != nil {
		t.Fatalf("third Add: %v", err)
	}
	if jobs.count != 1 {
		t.Fatalf("enqueued=%d want 1 after success", jobs.count)
	}
}

func TestReportJobOutcomeReleasesOnPermanentFailure(t *testing.T) {
	t.Parallel()

	jobs := &fakeJobs{}
	idem := newFakeIdem()
	svc := New(&fakeUpstream{}, jobs, Options{AddAsync: true, Idem: idem}, nil)
	req := domain.AddRequest{Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`), Async: true}
	if _, err := svc.Add(context.Background(), caller(), req); err != nil {
		t.Fatalf("Add: %v", err)
	}
	payload := jobs.job.Payload

	svc.ReportJobOutcome(context.Background(), payload, errors.New("worker failed"))
	if len(idem.records) != 0 {
		t.Fatalf("terminal failure should release record: %+v", idem.records)
	}
	if _, err := svc.Add(context.Background(), caller(), req); err != nil {
		t.Fatalf("retry Add: %v", err)
	}
	if jobs.count != 2 {
		t.Fatalf("enqueued=%d want 2 after release", jobs.count)
	}
}

func TestAddAsyncAndFallback(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	jobs := &fakeJobs{}
	svc := New(up, jobs, Options{AddAsync: true}, nil)
	result, err := svc.Add(context.Background(), caller(), domain.AddRequest{
		Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`), Async: true,
	})
	if err != nil {
		t.Fatalf("Add: %v", err)
	}
	if !strings.Contains(result.Message, "background") || jobs.job.Kind != domain.JobKindMemoryAdd {
		t.Fatalf("result=%+v job=%+v", result, jobs.job)
	}
	if up.add.TenantID != "" {
		t.Fatal("async add should not call upstream")
	}
	if _, err := svc.RunAddJob(context.Background(), jobs.job.Payload); err != nil {
		t.Fatalf("RunAddJob: %v", err)
	}
	if up.add.TenantID != "cube-a" {
		t.Fatalf("job payload lost tenant: %+v", up.add)
	}

	// 入队失败退回同步。
	jobs.err = errors.New("db down")
	if _, err := svc.Add(context.Background(), caller(), domain.AddRequest{
		Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`), Async: true,
	}); err != nil {
		t.Fatalf("fallback Add: %v", err)
	}
	if up.add.TenantID != "cube-a" {
		t.Fatal("fallback did not write synchronously")
	}
}

// 异步入队时 job.ID 优先沿用请求审计行的 call_id，worker 终态行才能与请求行对上。
func TestAddAsyncUsesCallIDAsJobID(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name      string
		callID    string
		generated bool
	}{
		{name: "沿用请求行的 call_id", callID: "call-1"},
		{name: "缺省时生成", callID: "", generated: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			jobs := &fakeJobs{}
			svc := New(&fakeUpstream{}, jobs, Options{AddAsync: true}, nil)
			result, err := svc.Add(context.Background(), caller(), domain.AddRequest{
				Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`), Async: true, CallID: tc.callID,
			})
			if err != nil {
				t.Fatalf("Add: %v", err)
			}
			if !result.Deferred {
				t.Fatalf("应走异步: %+v", result)
			}
			if result.CallID != jobs.job.ID {
				t.Fatalf("result.CallID = %q want job.ID = %q", result.CallID, jobs.job.ID)
			}
			if tc.generated {
				if jobs.job.ID == "" {
					t.Fatal("call_id 为空时应生成 job ID")
				}
				return
			}
			if jobs.job.ID != tc.callID {
				t.Fatalf("job.ID = %q want %q", jobs.job.ID, tc.callID)
			}
		})
	}
}

// 同步路径不使用 call_id，也不入队。
func TestAddSyncIgnoresCallID(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	jobs := &fakeJobs{}
	svc := New(up, jobs, Options{AddAsync: true}, nil)
	if _, err := svc.Add(context.Background(), caller(), domain.AddRequest{
		Messages: json.RawMessage(`[{"role":"user","content":"hi"}]`), CallID: "call-sync",
	}); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if jobs.count != 0 {
		t.Fatalf("同步 add 不应入队，enqueued=%d", jobs.count)
	}
	if up.add.TenantID != "cube-a" {
		t.Fatalf("应同步写入上游: %+v", up.add)
	}
}

func TestSearchClampsAndResolvesSession(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{}
	svc := New(up, nil, Options{DefaultTopK: 10, MaxTopK: 50, QueryPrefix: "prefix ", DreamTopK: 3}, nil)

	if _, err := svc.Search(context.Background(), caller(), domain.SearchQuery{Text: "  "}); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("empty query err = %v", err)
	}
	if _, err := svc.Search(context.Background(), caller(), domain.SearchQuery{
		Text: "hello", TopK: 999, SessionID: "s1",
	}); err != nil {
		t.Fatalf("Search: %v", err)
	}
	if up.search.TopK != 50 || up.search.DreamTopK != 3 || up.search.TenantID != "cube-a" {
		t.Fatalf("query = %+v", up.search)
	}
	if up.search.Text != "prefix hello" || up.search.SessionID != "s1" {
		t.Fatalf("query text/session = %q / %q", up.search.Text, up.search.SessionID)
	}
}

func TestDeleteScopedToTenant(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{owned: map[string]bool{"a": true, "c": true, "b": true}}
	svc := New(up, nil, Options{}, nil)

	result, err := svc.Delete(context.Background(), caller(), []string{"a", "c", "a", " "})
	if err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if len(result.DeletedIDs) != 2 || len(up.deleted) != 2 || up.deleted[0] != "a" || up.deleted[1] != "c" {
		t.Fatalf("result = %+v deleted=%+v", result, up.deleted)
	}
	if up.deletedTenant != "cube-a" {
		t.Fatalf("上游删除必须带租户作用域, got %q", up.deletedTenant)
	}

	// 不存在的 id：成功删除自有记忆，missing 回传 not_found_ids。
	up = &fakeUpstream{owned: map[string]bool{"a": true}}
	svc = New(up, nil, Options{}, nil)
	result, err = svc.Delete(context.Background(), caller(), []string{"a", "b"})
	if err != nil {
		t.Fatalf("missing id err = %v", err)
	}
	if len(result.DeletedIDs) != 1 || len(result.NotFoundIDs) != 1 || result.NotFoundIDs[0] != "b" {
		t.Fatalf("missing result = %+v", result)
	}
	if len(up.deleted) != 1 || up.deleted[0] != "a" {
		t.Fatalf("missing 不应阻止自有删除: %+v", up.deleted)
	}

	// 归属是租户而不是登录用户名：同租户的 unknown（软删/重试）按 missing 处理。
	up = &fakeUpstream{owned: map[string]bool{"a": true}, owners: map[string]string{"b": "cube-a"}}
	svc = New(up, nil, Options{}, nil)
	result, err = svc.Delete(context.Background(), caller(), []string{"a", "b"})
	if err != nil || len(result.NotFoundIDs) != 1 {
		t.Fatalf("same-tenant unknown = %+v, %v", result, err)
	}

	// 别人的 id：整批拒绝，且不调用上游删除。
	up = &fakeUpstream{owned: map[string]bool{"a": true}, owners: map[string]string{"b": "bob"}}
	svc = New(up, nil, Options{}, nil)
	if _, err := svc.Delete(context.Background(), caller(), []string{"a", "b"}); !errors.Is(err, domain.ErrForbidden) {
		t.Fatalf("foreign id err = %v", err)
	}
	if len(up.deleted) != 0 {
		t.Fatalf("整批拒绝时不应删除: %+v", up.deleted)
	}

	// 归属查询失败：fail-closed，不删除。
	up = &fakeUpstream{owned: map[string]bool{"a": true}, ownersErr: domain.Wrap(domain.ErrUpstreamUnavailable, "owners", "抖动", nil)}
	svc = New(up, nil, Options{}, nil)
	if _, err := svc.Delete(context.Background(), caller(), []string{"a", "b"}); !errors.Is(err, domain.ErrUpstreamUnavailable) {
		t.Fatalf("owners err = %v", err)
	}
	if len(up.deleted) != 0 {
		t.Fatalf("归属未知时不应删除: %+v", up.deleted)
	}
	if _, err := svc.Delete(context.Background(), caller(), nil); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("empty ids err = %v", err)
	}
	admin := domain.Caller{Admin: true}
	if _, err := svc.Delete(context.Background(), admin, []string{"a"}); !errors.Is(err, domain.ErrForbidden) {
		t.Fatalf("admin err = %v", err)
	}
}

func TestFeedbackAndProfile(t *testing.T) {
	t.Parallel()

	up := &fakeUpstream{feedbackResult: domain.FeedbackResult{Status: "success", Message: "ok"}, owned: map[string]bool{"m1": true}}
	svc := New(up, nil, Options{}, nil)

	if _, err := svc.Feedback(context.Background(), caller(), domain.FeedbackRequest{Content: " "}); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("empty feedback err = %v", err)
	}
	if _, err := svc.Feedback(context.Background(), caller(), domain.FeedbackRequest{
		Content: "用户想改成 Y", FirstMessage: "会话一", RetrievedMemoryIDs: []string{"m1"},
	}); err != nil {
		t.Fatalf("Feedback: %v", err)
	}
	if up.feedback.TenantID != "cube-a" || up.feedback.SessionID != ResolveSessionID("", "会话一") {
		t.Fatalf("feedback = %+v", up.feedback)
	}

	if _, err := svc.Profile(context.Background(), caller(), domain.ProfileQuery{Page: 0, PageSize: 999}); err != nil {
		t.Fatalf("Profile: %v", err)
	}
	if up.profile.Page != 1 || up.profile.PageSize != 50 || up.profile.TenantID != "cube-a" {
		t.Fatalf("profile = %+v", up.profile)
	}
}
