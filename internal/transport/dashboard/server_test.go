package dashboard

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
)

const adminToken = "memos_dashboard_admin"

func TestDashboardRequiresAdminToken(t *testing.T) {
	srv := newTestServer(t)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/api/dashboard/project/dict", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if bytes.Contains(rec.Body.Bytes(), []byte(adminToken)) {
		t.Fatalf("响应泄漏 token")
	}
}

func TestProjectLifecycle(t *testing.T) {
	srv := newTestServer(t)
	create := call(t, srv, http.MethodPost, "/api/dashboard/project/create",
		`{"name":"cube-new","description":"第一个项目","isMultiView":1,"ownerUsername":"alice"}`)
	if create.Code != http.StatusOK || !bytes.Contains(create.Body.Bytes(), []byte(`"status":"ENABLED"`)) {
		t.Fatalf("create: %d %s", create.Code, create.Body.String())
	}
	var created struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(create.Body.Bytes(), &created); err != nil {
		t.Fatalf("decode create: %v", err)
	}
	if created.Data["description"] != "第一个项目" || created.Data["isMultiView"] != float64(1) {
		t.Fatalf("create 应回显持久化后的描述与多视图: %v", created.Data)
	}

	// 不带这两个字段的更新不应把它们清空。
	update := call(t, srv, http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-new","name":"cube-renamed"}`)
	if update.Code != http.StatusOK {
		t.Fatalf("update: %d %s", update.Code, update.Body.String())
	}
	var renamed struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(update.Body.Bytes(), &renamed); err != nil {
		t.Fatalf("decode update: %v", err)
	}
	if renamed.Data["name"] != "cube-renamed" || renamed.Data["description"] != "第一个项目" || renamed.Data["isMultiView"] != float64(1) {
		t.Fatalf("改名不应清空描述/多视图: %v", renamed.Data)
	}

	meta := call(t, srv, http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-new","description":"","isMultiView":0}`)
	if meta.Code != http.StatusOK {
		t.Fatalf("meta update: %d %s", meta.Code, meta.Body.String())
	}
	var metaUpdated struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(meta.Body.Bytes(), &metaUpdated); err != nil {
		t.Fatalf("decode meta update: %v", err)
	}
	if metaUpdated.Data["description"] != "" || metaUpdated.Data["isMultiView"] != float64(0) {
		t.Fatalf("描述/多视图应可清空: %v", metaUpdated.Data)
	}

	list := call(t, srv, http.MethodPost, "/api/dashboard/project/list", `{"pageNum":1,"pageSize":10}`)
	if !bytes.Contains(list.Body.Bytes(), []byte(`"records"`)) || !bytes.Contains(list.Body.Bytes(), []byte(`"cube-new"`)) {
		t.Fatalf("list: %s", list.Body.String())
	}
	revoke := call(t, srv, http.MethodPost, "/api/dashboard/project/revoke/cube-new", "")
	if revoke.Code != http.StatusOK {
		t.Fatalf("revoke: %d %s", revoke.Code, revoke.Body.String())
	}
	dict := call(t, srv, http.MethodGet, "/api/dashboard/project/dict", "")
	if bytes.Contains(dict.Body.Bytes(), []byte(`"id":"cube-new"`)) {
		t.Fatalf("停用项目不应再出现在字典里: %s", dict.Body.String())
	}
}

func TestProjectArchiveAndRestore(t *testing.T) {
	srv := newTestServer(t)
	create := call(t, srv, http.MethodPost, "/api/dashboard/project/create", `{"name":"cube-gone","ownerUsername":"alice"}`)
	if create.Code != http.StatusOK {
		t.Fatalf("create: %d %s", create.Code, create.Body.String())
	}
	del := call(t, srv, http.MethodPost, "/api/dashboard/project/delete/cube-gone", "")
	if del.Code != http.StatusOK || !bytes.Contains(del.Body.Bytes(), []byte(`"archived":true`)) {
		t.Fatalf("删除应归档: %d %s", del.Code, del.Body.String())
	}
	dict := call(t, srv, http.MethodGet, "/api/dashboard/project/dict", "")
	if bytes.Contains(dict.Body.Bytes(), []byte("cube-gone")) {
		t.Fatalf("归档后不应再出现在字典里: %s", dict.Body.String())
	}
	list := call(t, srv, http.MethodPost, "/api/dashboard/project/list", `{"pageNum":1,"pageSize":10}`)
	if !bytes.Contains(list.Body.Bytes(), []byte("cube-gone")) {
		t.Fatalf("项目页仍应能看到归档项目: %s", list.Body.String())
	}
	// 归档是软删除：重复归档幂等，恢复后项目重新可用。
	if again := call(t, srv, http.MethodPost, "/api/dashboard/project/delete/cube-gone", ""); again.Code != http.StatusOK {
		t.Fatalf("重复归档应幂等: %d %s", again.Code, again.Body.String())
	}
	if restored := call(t, srv, http.MethodPost, "/api/dashboard/project/restore/cube-gone", ""); restored.Code != http.StatusOK {
		t.Fatalf("恢复: %d %s", restored.Code, restored.Body.String())
	}
	dict = call(t, srv, http.MethodGet, "/api/dashboard/project/dict", "")
	if !bytes.Contains(dict.Body.Bytes(), []byte("cube-gone")) {
		t.Fatalf("恢复后应回到字典: %s", dict.Body.String())
	}
	if restored := call(t, srv, http.MethodPost, "/api/dashboard/project/restore/cube-gone", ""); restored.Code != http.StatusConflict {
		t.Fatalf("重复恢复应 409: %d %s", restored.Code, restored.Body.String())
	}
}

// 启用与切换是两个动作：恢复被停用的项目不该改变调用方正在看的项目。
func TestProjectEnableDoesNotSwitchCurrent(t *testing.T) {
	srv := newTestServer(t)
	testAccounts.switches = nil

	if code := call(t, srv, http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-a","status":"DISABLED"}`); code.Code != http.StatusOK {
		t.Fatalf("disable: %d %s", code.Code, code.Body.String())
	}
	if code := call(t, srv, http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-a","status":"ENABLED"}`); code.Code != http.StatusOK {
		t.Fatalf("re-enable: %d %s", code.Code, code.Body.String())
	}
	if len(testAccounts.switches) != 0 {
		t.Fatalf("启用项目不应切换当前项目，实际切到了 %v", testAccounts.switches)
	}
}

// 切换要显式带 current=true；只有它才会走 SwitchProject。
func TestProjectSelectSwitchesCurrent(t *testing.T) {
	srv := newTestServer(t)
	testAccounts.switches = nil

	if code := call(t, srv, http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-a","current":true}`); code.Code != http.StatusOK {
		t.Fatalf("select: %d %s", code.Code, code.Body.String())
	}
	if len(testAccounts.switches) != 1 || testAccounts.switches[0] != "cube-a" {
		t.Fatalf("current=true 应切换项目，实际 %v", testAccounts.switches)
	}
}

func TestProjectReEnable(t *testing.T) {
	srv := newTestServer(t)
	create := call(t, srv, http.MethodPost, "/api/dashboard/project/create", `{"name":"cube-reenable","ownerUsername":"alice"}`)
	if create.Code != http.StatusOK {
		t.Fatalf("create: %d %s", create.Code, create.Body.String())
	}
	if revoke := call(t, srv, http.MethodPost, "/api/dashboard/project/revoke/cube-reenable", ""); revoke.Code != http.StatusOK {
		t.Fatalf("revoke: %d %s", revoke.Code, revoke.Body.String())
	}
	// 停用后必须能恢复：以前 status=ENABLED 会走 SwitchProject 被“项目空间已停用”拒绝，
	// 停用成了不可逆操作。
	if re := call(t, srv, http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-reenable","status":"ENABLED"}`); re.Code != http.StatusOK {
		t.Fatalf("re-enable: %d %s", re.Code, re.Body.String())
	}
	dict := call(t, srv, http.MethodGet, "/api/dashboard/project/dict", "")
	if !bytes.Contains(dict.Body.Bytes(), []byte(`"id":"cube-reenable"`)) {
		t.Fatalf("dict: %s", dict.Body.String())
	}
	if bytes.Contains(dict.Body.Bytes(), []byte(`"status":"DISABLED"`)) {
		t.Fatalf("恢复后不应再是 DISABLED: %s", dict.Body.String())
	}
}

func TestProjectDeleteRejectsPersonal(t *testing.T) {
	srv, store := newTestServerStore(t, nil)
	store.tenants["u-acct-a"] = domain.Tenant{ID: "u-acct-a", UserID: "u-acct-a", Kind: domain.TenantKindPersonal, CreatedByAccountID: "acct-a"}
	del := call(t, srv, http.MethodPost, "/api/dashboard/project/delete/u-acct-a", "")
	if del.Code != http.StatusConflict {
		t.Fatalf("个人项目删除应 409: %d %s", del.Code, del.Body.String())
	}
}

func TestProfileTemplateRoutes(t *testing.T) {
	srv := newTestServer(t)
	fake, ok := srv.deps.Templates.(*fakeTemplates)
	if !ok {
		t.Fatal("测试服务器应装配 fakeTemplates")
	}
	detail := call(t, srv, http.MethodGet, "/api/dashboard/profileTemplate/detail?templateId=t1&projectId=cube-a", "")
	var env struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(detail.Body.Bytes(), &env); err != nil || detail.Code != http.StatusOK {
		t.Fatalf("detail: %d %s", detail.Code, detail.Body.String())
	}
	if env.Data["templateId"] != "t1" || env.Data["projectId"] != "cube-a" {
		t.Fatalf("身份字段不能被属性分组覆盖: %v", env.Data)
	}
	for _, step := range []struct {
		path, body string
		want       int
	}{
		{"create", `{"projectId":"cube-a","templateName":"n"}`, http.StatusBadRequest},
		{"create", `{"projectId":"cube-a","templateName":"n","基础":{"年龄":{"value":"int","algorithm_updatable":true}}}`, http.StatusOK},
		{"update", `{"projectId":"cube-a","templateId":"t1"}`, http.StatusBadRequest},
		{"update", `{"projectId":"cube-a","templateId":"t1","templateName":"n2"}`, http.StatusOK},
		{"delete", `{"projectId":"cube-a","templateId":"t1"}`, http.StatusOK},
	} {
		if got := call(t, srv, http.MethodPost, "/api/dashboard/profileTemplate/"+step.path, step.body); got.Code != step.want {
			t.Fatalf("%s: status=%d want=%d body=%s", step.path, got.Code, step.want, got.Body.String())
		}
	}
	// 缺分组的创建与空更新都在本地拒绝，不会打到上游；其余 4 次调用都必须带已授权的项目 ID。
	if len(fake.tenants) != 4 {
		t.Fatalf("上游端口调用次数=%d want=4", len(fake.tenants))
	}
	for _, tenant := range fake.tenants {
		if tenant != "cube-a" {
			t.Fatalf("上游端口收到的租户=%q", tenant)
		}
	}
}

// mustBool 把 JSON 解出来的 any 断言成 bool，避免测试里写 `x != true` 这种字面量比较。
func mustBool(t *testing.T, v any) bool {
	t.Helper()
	b, ok := v.(bool)
	if !ok {
		t.Fatalf("字段不是布尔: %#v", v)
	}
	return b
}

// 密钥明文落库（迁移 0024）后：有写权限的主体在列表里就能再次复制，copy 端点也返回
// 明文；历史行（raw 为空）与无写权限的主体仍只拿前缀掩码。
func TestKeysCreateListAndCopy(t *testing.T) {
	srv := newTestServer(t)

	decode := func(rec *httptest.ResponseRecorder) map[string]any {
		t.Helper()
		var env struct {
			Data map[string]any `json:"data"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
			t.Fatalf("decode %s: %v", rec.Body.String(), err)
		}
		return env.Data
	}

	created := decode(call(t, srv, http.MethodPost, "/api/dashboard/keys/create", `{"projectId":"cube-a","keyName":"ci"}`))
	raw, _ := created["apiKey"].(string)
	if !strings.HasPrefix(raw, "sk-") {
		t.Fatalf("创建响应未返回明文: %v", created)
	}
	if !mustBool(t, created["canCopy"]) {
		t.Fatalf("创建响应应标记 canCopy: %v", created)
	}
	rawID, _ := created["id"].(string)

	// 列表**不下发明文**，只给掩码 + canCopy；明文按需走 keys/copy。
	list := call(t, srv, http.MethodPost, "/api/dashboard/keys/list", `{"projectId":"cube-a"}`)
	records, _ := decode(list)["records"].([]any)
	if len(records) != 1 {
		t.Fatalf("列表条数=%d body=%s", len(records), list.Body.String())
	}
	row, _ := records[0].(map[string]any)
	listed, _ := row["apiKey"].(string)
	if strings.Contains(listed, raw) || !strings.HasSuffix(listed, "…") {
		t.Fatalf("列表应只有掩码: %v", row)
	}
	if !mustBool(t, row["canCopy"]) {
		t.Fatalf("有写权限时 canCopy 应为真: %v", row)
	}

	// copy 端点按 id 返回明文。
	copied := decode(call(t, srv, http.MethodPost, "/api/dashboard/keys/copy", `{"apiKeyId":"`+rawID+`"}`))
	if copied["apiKey"] != raw {
		t.Fatalf("copy 明文不符: %v", copied)
	}
	if missing := call(t, srv, http.MethodPost, "/api/dashboard/keys/copy", `{"apiKeyId":"nope"}`); missing.Code != http.StatusNotFound {
		t.Fatalf("不存在的 Key 应 404，got %d %s", missing.Code, missing.Body.String())
	}
}

// canCopy 为假、或库里没有明文（迁移前的历史行、旧网关导入的 Key）时只给前缀掩码：
// 空明文绝不能被当成“明文就是空串”下发。
func TestKeyJSONFallsBackToMask(t *testing.T) {
	t.Parallel()

	tok := domain.Token{ID: "k1", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice", Prefix: "memos_c8", Raw: "memos_c8full"}

	hidden := keyJSON(tok, false)
	if hidden["apiKey"] != "memos_c8…" || mustBool(t, hidden["canCopy"]) {
		t.Fatalf("无写权限应只给掩码且不可复制: %v", hidden)
	}

	legacy := tok
	legacy.Raw = ""
	fallback := keyJSON(legacy, true)
	if fallback["apiKey"] != "memos_c8…" || mustBool(t, fallback["canCopy"]) {
		t.Fatalf("没有明文的历史行应只给掩码且不可复制: %v", fallback)
	}

	// 列表形态始终掩码，canCopy 只表示「能按需取到明文」。
	listed := keyJSON(tok, true)
	if listed["apiKey"] != "memos_c8…" || !mustBool(t, listed["canCopy"]) {
		t.Fatalf("列表形态应给掩码 + canCopy: %v", listed)
	}

	// 创建响应是明文唯一一次进入响应体的地方。
	issued := keyJSONIssued(tok)
	if issued["apiKey"] != "memos_c8full" || !mustBool(t, issued["canCopy"]) {
		t.Fatalf("签发响应应带明文: %v", issued)
	}
	if issuedLegacy := keyJSONIssued(legacy); issuedLegacy["apiKey"] != "memos_c8…" {
		t.Fatalf("没有明文时签发响应也不该给空串: %v", issuedLegacy)
	}
}

func TestKeyCreateExpiry(t *testing.T) {
	t.Parallel()
	srv := newTestServer(t)

	createKey := func(body string) *httptest.ResponseRecorder {
		return call(t, srv, http.MethodPost, "/api/dashboard/keys/create", body)
	}
	decode := func(rec *httptest.ResponseRecorder) map[string]any {
		t.Helper()
		var env struct {
			Data map[string]any `json:"data"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
			t.Fatalf("decode: %v body=%s", err, rec.Body.String())
		}
		return env.Data
	}

	withExpiry := createKey(`{"projectId":"cube-a","keyName":"seven-days","expireDays":7}`)
	if withExpiry.Code != http.StatusOK {
		t.Fatalf("带 expireDays 创建: %d %s", withExpiry.Code, withExpiry.Body.String())
	}
	data := decode(withExpiry)
	expireTime, _ := data["expireTime"].(string)
	parsed, err := time.ParseInLocation(domain.ClockLayout, expireTime, time.FixedZone("CST", 8*3600))
	if err != nil {
		t.Fatalf("expireTime = %q: %v", expireTime, err)
	}
	if delta := time.Until(parsed); delta < 6*24*time.Hour || delta > 8*24*time.Hour {
		t.Fatalf("expireTime 与 7 天不符: %q (delta=%v)", expireTime, delta)
	}

	tokens, err := srv.deps.Tokens.ListTokens(context.Background(), "cube-a")
	if err != nil {
		t.Fatalf("ListTokens: %v", err)
	}
	var stored *domain.Token
	for i := range tokens {
		if tokens[i].Label == "seven-days" {
			stored = &tokens[i]
		}
	}
	if stored == nil || stored.ExpiresAt == nil {
		t.Fatalf("带 expireDays 的 Token 落库应有过期时间: %+v", tokens)
	}

	forever := createKey(`{"projectId":"cube-a","keyName":"forever"}`)
	if forever.Code != http.StatusOK {
		t.Fatalf("不带 expireDays 创建: %d %s", forever.Code, forever.Body.String())
	}
	if got := decode(forever)["expireTime"]; got != "" {
		t.Fatalf("永不过期的 expireTime 应为空串: %v", got)
	}
	zero := createKey(`{"projectId":"cube-a","keyName":"zero-days","expireDays":0}`)
	if zero.Code != http.StatusOK || decode(zero)["expireTime"] != "" {
		t.Fatalf("expireDays=0 应永不过期: %d %s", zero.Code, zero.Body.String())
	}

	for _, body := range []string{
		`{"projectId":"cube-a","keyName":"bad","expireDays":-1}`,
		`{"projectId":"cube-a","keyName":"bad","expireDays":3651}`,
		`{"projectId":"cube-a","keyName":"bad","expireDays":1.5}`,
		`{"projectId":"cube-a","keyName":"bad","expireDays":"abc"}`,
		`{"projectId":"cube-a","keyName":"bad","expireDays":true}`,
	} {
		if rec := createKey(body); rec.Code != http.StatusBadRequest {
			t.Fatalf("非法 expireDays 应 400: %s -> %d %s", body, rec.Code, rec.Body.String())
		}
	}
}

func TestMemoryDetailAndDelete(t *testing.T) {
	srv := newTestServer(t)
	detail := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/detail", `{"projectId":"cube-a","memoryId":"mem-1"}`)
	if detail.Code != http.StatusOK || !bytes.Contains(detail.Body.Bytes(), []byte(`"memoryContent":"tea"`)) {
		t.Fatalf("detail: %d %s", detail.Code, detail.Body.String())
	}
	foreign := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/detail", `{"projectId":"other","memoryId":"mem-1"}`)
	if foreign.Code != http.StatusNotFound {
		t.Fatalf("foreign detail: %d %s", foreign.Code, foreign.Body.String())
	}
	del := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/delete", `{"projectId":"cube-a","memory_ids":["mem-1"]}`)
	if del.Code != http.StatusOK || !bytes.Contains(del.Body.Bytes(), []byte(`"deleted":1`)) {
		t.Fatalf("delete: %d %s", del.Code, del.Body.String())
	}
	event := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/delete", `{"projectId":"cube-a","memory_ids":["mem-1"],"memType":"event_mem"}`)
	if event.Code != http.StatusBadRequest {
		t.Fatalf("event delete: %d %s", event.Code, event.Body.String())
	}
}

// prompt 五条代理上游的项目级配置：读默认值、保存、重置、生成、干跑。
func TestPromptConfig(t *testing.T) {
	srv := newTestServer(t)

	detail := call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/detail", `{"projectId":"cube-a"}`)
	if detail.Code != http.StatusOK || !bytes.Contains(detail.Body.Bytes(), []byte(`"isDefault":true`)) {
		t.Fatalf("detail: %d %s", detail.Code, detail.Body.String())
	}

	save := call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/save",
		`{"projectId":"cube-a","prompt":"自定义 ${conversation}"}`)
	if save.Code != http.StatusOK || !bytes.Contains(save.Body.Bytes(), []byte(`"ok":true`)) {
		t.Fatalf("save: %d %s", save.Code, save.Body.String())
	}
	detail = call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/detail", `{"projectId":"cube-a"}`)
	if !bytes.Contains(detail.Body.Bytes(), []byte(`自定义 ${conversation}`)) {
		t.Fatalf("detail after save: %s", detail.Body.String())
	}

	empty := call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/save", `{"projectId":"cube-a"}`)
	if empty.Code != http.StatusBadRequest {
		t.Fatalf("empty prompt: %d %s", empty.Code, empty.Body.String())
	}

	reset := call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/reset-default", `{"projectId":"cube-a"}`)
	if reset.Code != http.StatusOK || !bytes.Contains(reset.Body.Bytes(), []byte(fakeDefaultPrompt)) {
		t.Fatalf("reset: %d %s", reset.Code, reset.Body.String())
	}

	generated := call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/generate",
		`{"projectId":"cube-a","description":"只抽安全事件"}`)
	if generated.Code != http.StatusOK || !bytes.Contains(generated.Body.Bytes(), []byte(`重写(只抽安全事件)`)) {
		t.Fatalf("generate: %d %s", generated.Code, generated.Body.String())
	}

	debug := call(t, srv, http.MethodPost, "/api/dashboard/prompt-config/debug/extract",
		`{"projectId":"cube-a","messages":[{"role":"user","content":"我喜欢深色模式"}]}`)
	if debug.Code != http.StatusOK || !bytes.Contains(debug.Body.Bytes(), []byte(`"content":"我喜欢深色模式"`)) {
		t.Fatalf("debug: %d %s", debug.Code, debug.Body.String())
	}
}

func TestModelsGraphAndMeta(t *testing.T) {
	srv := newTestServer(t)
	models := call(t, srv, http.MethodGet, "/api/dashboard/models", "")
	if models.Code != http.StatusOK || !bytes.Contains(models.Body.Bytes(), []byte(`"total":2`)) {
		t.Fatalf("models: %d %s", models.Code, models.Body.String())
	}
	graph := call(t, srv, http.MethodGet, "/api/dashboard/graph/memories?tenant=cube-a", "")
	if graph.Code != http.StatusOK || !bytes.Contains(graph.Body.Bytes(), []byte(`"nodes"`)) {
		t.Fatalf("graph: %d %s", graph.Code, graph.Body.String())
	}
	meta := call(t, srv, http.MethodGet, "/api/meta", "")
	if meta.Code != http.StatusOK || !bytes.Contains(meta.Body.Bytes(), []byte(`"mode":"live"`)) {
		t.Fatalf("meta: %d %s", meta.Code, meta.Body.String())
	}
}

func TestMetaReportsLiveSourceStatus(t *testing.T) {
	srv := newTestServer(t)
	meta := call(t, srv, http.MethodGet, "/api/meta", "")
	if meta.Code != http.StatusOK {
		t.Fatalf("meta: %d %s", meta.Code, meta.Body.String())
	}
	var env struct {
		Data struct {
			Health  map[string]any `json:"health"`
			Sources map[string]any `json:"sources"`
		} `json:"data"`
	}
	if err := json.Unmarshal(meta.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode meta: %v", err)
	}
	if env.Data.Sources["upstream"] != "ok" || env.Data.Sources["graph"] != "ok" {
		t.Fatalf("数据源应反映真实探测结果: %+v", env.Data.Sources)
	}
	if env.Data.Health["status"] != "healthy" || env.Data.Health["service"] != "memos" {
		t.Fatalf("health 应回上游 /health 原始 JSON: %v", env.Data.Health)
	}
	if got, _ := env.Data.Sources["keys"].(string); !strings.Contains(got, "把") {
		t.Fatalf("keys 应给出真实数量: %v", env.Data.Sources["keys"])
	}
}

// TestMetaAuthMode 钉住 authMode 的来源：未配置按 page，零信任必选时为 external。
func TestHealthReportsLiveChecks(t *testing.T) {
	srv := newTestServer(t)
	rec := call(t, srv, http.MethodGet, "/api/health", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("health: %d %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data struct {
			Status    string      `json:"status"`
			Gateway   healthCheck `json:"gateway"`
			Upstream  healthCheck `json:"upstream"`
			CheckedAt string      `json:"checkedAt"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode health: %v", err)
	}
	if env.Data.Status != "ok" || !env.Data.Gateway.OK || !env.Data.Upstream.OK {
		t.Fatalf("两边都应正常: %+v", env.Data)
	}
	if _, err := time.Parse(time.RFC3339, env.Data.CheckedAt); err != nil {
		t.Fatalf("checkedAt 应为 RFC3339: %q", env.Data.CheckedAt)
	}

	// 上游挂了、网关正常 → degraded，不能把整盏灯报成正常。
	srv.deps.Memories = &failingUpstream{}
	rec = call(t, srv, http.MethodGet, "/api/health", "")
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode degraded health: %v", err)
	}
	if env.Data.Status != "degraded" || !env.Data.Gateway.OK || env.Data.Upstream.OK {
		t.Fatalf("上游失败时应为 degraded: %+v", env.Data)
	}
}

type failingUpstream struct{ fakeUpstream }

func (f *failingUpstream) Health(context.Context) (map[string]any, error) {
	return nil, errors.New("boom")
}

func TestMetaAuthMode(t *testing.T) {
	t.Parallel()

	if got := newTestServer(t).deps.AuthMode; got != AuthModePage {
		t.Fatalf("默认 authMode = %q，期望 %q", got, AuthModePage)
	}

	srv := newTestServer(t)
	srv.deps.AuthMode = AuthModeExternal
	meta := call(t, srv, http.MethodGet, "/api/meta", "")
	if meta.Code != http.StatusOK {
		t.Fatalf("meta: %d %s", meta.Code, meta.Body.String())
	}
	var env struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(meta.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode meta: %v", err)
	}
	if env.Data["authMode"] != AuthModeExternal {
		t.Fatalf("authMode = %v，期望 %q", env.Data["authMode"], AuthModeExternal)
	}
	if enabled, ok := env.Data["authEnabled"].(bool); !ok || !enabled {
		t.Fatalf("authEnabled = %v，期望 true", env.Data["authEnabled"])
	}
}

// TestTenantWriteInvalidatesAuthCache 停用/回收项目后必须清空主体缓存，权限立即生效。
func TestTenantWriteInvalidatesAuthCache(t *testing.T) {
	t.Parallel()
	var calls int
	srv := newTestServerWith(t, func() { calls++ })

	if rec := call(t, srv, http.MethodPost, "/api/dashboard/project/update",
		`{"id":"cube-a","status":"DISABLED"}`); rec.Code != http.StatusOK {
		t.Fatalf("disable: %d %s", rec.Code, rec.Body.String())
	}
	if rec := call(t, srv, http.MethodPost, "/api/dashboard/project/revoke/cube-a", ""); rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d %s", rec.Code, rec.Body.String())
	}
	if calls != 2 {
		t.Fatalf("InvalidateAuth 调用次数 = %d，期望 2", calls)
	}
}

func call(t *testing.T, srv *Server, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	return callAs(t, srv, adminToken, method, path, body)
}

// callAs 用指定 Bearer 调一次 Dashboard 路由；用于角色矩阵测试。
func callAs(t *testing.T, srv *Server, token, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	reader := bytes.NewBuffer(nil)
	if body != "" {
		reader = bytes.NewBufferString(body)
	}
	req := httptest.NewRequestWithContext(context.Background(), method, path, reader)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

const (
	viewerToken  = "memos_dashboard_viewer"
	sessionToken = "memos_dashboard_session"
)

// sessionAccounts 模拟带“当前项目”的会话主体：读路由若用 InBody，
// 缺 projectId 时会静默回落到 cube-a。
type sessionAccounts struct{}

func (sessionAccounts) Authenticate(_ context.Context, c domain.Credentials) (domain.Principal, error) {
	if c.Bearer != sessionToken {
		return domain.Principal{}, domain.Wrap(domain.ErrUnauthorized, "authenticate", "登录已过期，请重新登录", nil)
	}
	return domain.Principal{
		Kind:          domain.PrincipalAccount,
		Username:      "session-1",
		CurrentTenant: "cube-a",
		Roles:         map[string]domain.Role{"cube-a": domain.RoleViewer},
	}, nil
}

func (sessionAccounts) SwitchProject(context.Context, domain.Principal, string) error { return nil }

func (sessionAccounts) MemberRoles(context.Context, string) (map[string]domain.Role, error) {
	return map[string]domain.Role{"cube-a": domain.RoleViewer}, nil
}

// viewerAccounts 让测试用 viewer 身份调用：cube-a 里只有查看权限。
type viewerAccounts struct{}

func (viewerAccounts) Authenticate(_ context.Context, c domain.Credentials) (domain.Principal, error) {
	if c.Bearer != viewerToken {
		return domain.Principal{}, domain.Wrap(domain.ErrUnauthorized, "authenticate", "登录已过期，请重新登录", nil)
	}
	return domain.Principal{
		Kind:     domain.PrincipalAccount,
		Username: "viewer-1",
		Roles:    map[string]domain.Role{"cube-a": domain.RoleViewer},
	}, nil
}

func (viewerAccounts) SwitchProject(context.Context, domain.Principal, string) error { return nil }

func (viewerAccounts) MemberRoles(context.Context, string) (map[string]domain.Role, error) {
	return map[string]domain.Role{"cube-a": domain.RoleViewer}, nil
}

// adminOnly 只认管理 Token；账号、会话与角色矩阵由 internal/app 的黑盒 e2e 覆盖。
type adminOnly struct{}

// countingAccounts 记录 SwitchProject 的调用：用来区分「启用项目」和「切换项目」——
// 两者以前共用 status=ENABLED 一个信号，启用时会顺带把当前项目切过去。
type countingAccounts struct {
	adminOnly
	switches []string
}

func (c *countingAccounts) SwitchProject(_ context.Context, _ domain.Principal, id string) error {
	c.switches = append(c.switches, id)
	return nil
}

var testAccounts = &countingAccounts{}

func (adminOnly) Authenticate(_ context.Context, c domain.Credentials) (domain.Principal, error) {
	if c.Bearer != adminToken {
		return domain.Principal{}, domain.Wrap(domain.ErrUnauthorized, "authenticate", "登录已过期，请重新登录", nil)
	}
	return domain.Principal{Kind: domain.PrincipalAdminToken, Username: domain.AdminTokenUsername, PlatformAdmin: true}, nil
}

func (adminOnly) SwitchProject(context.Context, domain.Principal, string) error { return nil }

func (adminOnly) ResolveAccount(_ context.Context, ref string) (domain.Account, error) {
	if ref == "ghost" {
		return domain.Account{}, domain.NotFound("resolve_account", "账号不存在")
	}
	return domain.Account{ID: "acct-" + ref, Username: ref}, nil
}

func (adminOnly) MemberRoles(context.Context, string) (map[string]domain.Role, error) {
	return map[string]domain.Role{}, nil
}

// fakeKnowledge 给 Dashboard 路由提供稳定的知识库数据形状。
type fakeKnowledge struct{}

func (fakeKnowledge) KnowledgeBases(_ context.Context, _ string) ([]domain.KnowledgeBase, error) {
	return []domain.KnowledgeBase{{ID: "kb-1", Name: "手册", Description: "d", FileCount: 1, TotalSize: 12}}, nil
}

func (fakeKnowledge) KnowledgeBase(_ context.Context, _, id string) (domain.KnowledgeBase, error) {
	return domain.KnowledgeBase{ID: id, Name: "手册"}, nil
}

func (fakeKnowledge) CreateKnowledgeBase(_ context.Context, _, name, _ string) (domain.KnowledgeBase, error) {
	return domain.KnowledgeBase{ID: "kb-new", Name: name}, nil
}

func (fakeKnowledge) UpdateKnowledgeBase(_ context.Context, in domain.KnowledgeBaseUpdate) (domain.KnowledgeBase, error) {
	return domain.KnowledgeBase{ID: in.ID, Name: "renamed"}, nil
}

func (fakeKnowledge) DeleteKnowledgeBase(context.Context, string, string) error { return nil }

func (fakeKnowledge) KnowledgeFiles(_ context.Context, _ domain.KnowledgeFilesQuery) (domain.KnowledgeFilePage, error) {
	return domain.KnowledgeFilePage{Items: []domain.KnowledgeFile{{ID: "file-1", Name: "a.txt"}}, Total: 1}, nil
}

func (fakeKnowledge) AddKnowledgeFiles(_ context.Context, in domain.KnowledgeFilesAdd) ([]domain.KnowledgeFile, error) {
	out := make([]domain.KnowledgeFile, 0, len(in.Files))
	for _, f := range in.Files {
		out = append(out, domain.KnowledgeFile{ID: "file-new", Name: f.Name})
	}
	return out, nil
}

func (fakeKnowledge) DeleteKnowledgeFiles(context.Context, string, []string) (int, error) {
	return 1, nil
}

func (fakeKnowledge) DownloadKnowledgeFile(context.Context, string, string) ([]byte, string, error) {
	return []byte("data"), "a.txt", nil
}

func newTestServer(t *testing.T) *Server {
	t.Helper()
	return newTestServerWith(t, nil)
}

// newTestServerWith 允许测试观察项目写入后的鉴权缓存失效。
func newTestServerWith(t *testing.T, invalidate func()) *Server {
	t.Helper()
	srv, _ := newTestServerStore(t, invalidate)
	return srv
}

// newTestServerStore 同时返回底层假仓储，供测试直接造数据。
func newTestServerStore(t *testing.T, invalidate func()) (*Server, *fakeStore) {
	t.Helper()
	store := newFakeStore()
	store.tenants["cube-a"] = domain.Tenant{ID: "cube-a", UserID: "alice", Name: "cube-a", CreatedAt: time.Now()}
	ids := identity.New(store, nil, adminToken, slog.Default())
	if err := ids.Bootstrap(context.Background()); err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	up := &fakeUpstream{}
	gateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	t.Cleanup(gateway.Close)
	srv, err := New(Deps{
		Tenants:       tenant.New(store, testAccounts, invalidate),
		Tokens:        ids,
		Memory:        memory.New(up, nil, memory.Options{MaxTopK: 50}, slog.Default()),
		Memories:      up,
		Templates:     &fakeTemplates{},
		Prompts:       newFakePrompts(),
		PromptStudio:  &fakePromptGateway{},
		Models:        fakeModels{},
		Accounts:      testAccounts,
		Knowledge:     fakeKnowledge{},
		Graph:         &fakeGraph{},
		LocalSync:     fakeLocalSync{},
		Service:       "memos",
		GatewayURL:    gateway.URL,
		GraphMaxNodes: 4000,
		Logger:        slog.Default(),
	})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	return srv, store
}

type fakeStore struct {
	mu      sync.Mutex
	tenants map[string]domain.Tenant
	tokens  map[[32]byte]domain.Token
}

func newFakeStore() *fakeStore {
	return &fakeStore{tenants: map[string]domain.Tenant{}, tokens: map[[32]byte]domain.Token{}}
}

func (f *fakeStore) GetTenant(_ context.Context, id string) (domain.Tenant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	tenant, ok := f.tenants[id]
	if !ok {
		return domain.Tenant{}, domain.NotFound("get_tenant", "租户不存在")
	}
	return tenant, nil
}

func (f *fakeStore) CreateTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.tenants[t.ID]; ok {
		return domain.Wrap(domain.ErrConflict, "create_tenant", "已存在同名项目", nil)
	}
	if t.CreatedAt.IsZero() {
		t.CreatedAt = time.Now()
	}
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) CreateTenantWithOwner(ctx context.Context, t domain.Tenant, ownerAccountID string, role domain.Role) error {
	if !role.Valid() || ownerAccountID == "" {
		return domain.Invalid("create_tenant_with_owner", "owner 或角色不合法")
	}
	return f.CreateTenant(ctx, t)
}

func (f *fakeStore) UpsertTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if existing, ok := f.tenants[t.ID]; ok {
		t.CreatedAt = existing.CreatedAt
	} else if t.CreatedAt.IsZero() {
		t.CreatedAt = time.Now()
	}
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) EnsureTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if existing, ok := f.tenants[t.ID]; ok {
		t.CreatedAt = existing.CreatedAt
		t.Disabled = existing.Disabled
	} else if t.CreatedAt.IsZero() {
		t.CreatedAt = time.Now()
	}
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) ArchiveTenant(_ context.Context, id string, at time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	tenant, ok := f.tenants[id]
	if !ok {
		return domain.NotFound("archive_tenant", "租户不存在")
	}
	tenant.ArchivedAt = &at
	tenant.Disabled = true
	f.tenants[id] = tenant
	return nil
}

func (f *fakeStore) RestoreTenant(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	tenant, ok := f.tenants[id]
	if !ok {
		return domain.NotFound("restore_tenant", "租户不存在")
	}
	tenant.ArchivedAt = nil
	tenant.Disabled = false
	f.tenants[id] = tenant
	return nil
}

func (f *fakeStore) ListTenants(context.Context) ([]domain.Tenant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]domain.Tenant, 0, len(f.tenants))
	for id := range f.tenants {
		out = append(out, f.tenants[id])
	}
	return out, nil
}

func (f *fakeStore) UpsertToken(ctx context.Context, t domain.Token) error {
	return f.CreateToken(ctx, t)
}

func (f *fakeStore) CreateToken(_ context.Context, t domain.Token) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.tokens[t.Hash] = t
	return nil
}

func (f *fakeStore) TokenByHash(_ context.Context, hash [32]byte) (domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	token, ok := f.tokens[hash]
	if !ok {
		return domain.Token{}, domain.NotFound("token_by_hash", "")
	}
	return token, nil
}

func (f *fakeStore) GetToken(_ context.Context, id string) (domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for hash := range f.tokens {
		if f.tokens[hash].ID == id {
			return f.tokens[hash], nil
		}
	}
	return domain.Token{}, domain.NotFound("get_token", "")
}

func (f *fakeStore) ListTokens(_ context.Context, tenantID string) ([]domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]domain.Token, 0)
	for hash := range f.tokens {
		token := f.tokens[hash]
		if tenantID == "" || token.TenantID == tenantID {
			out = append(out, token)
		}
	}
	return out, nil
}

func (f *fakeStore) RevokeToken(_ context.Context, id string, at time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for hash := range f.tokens {
		if f.tokens[hash].ID == id {
			token := f.tokens[hash]
			token.RevokedAt = &at
			f.tokens[hash] = token
			return nil
		}
	}
	return domain.NotFound("revoke_token", "")
}

func (f *fakeStore) TouchToken(context.Context, string, time.Time) error { return nil }

func (f *fakeStore) UpdateTokenLabel(_ context.Context, id, label string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for hash := range f.tokens {
		if f.tokens[hash].ID == id {
			token := f.tokens[hash]
			token.Label = label
			f.tokens[hash] = token
			return nil
		}
	}
	return domain.NotFound("update_token_label", "")
}

type fakeUpstream struct {
	mu       sync.Mutex
	searches []domain.SearchQuery
}

func (f *fakeUpstream) Health(context.Context) (map[string]any, error) {
	return map[string]any{"status": "healthy", "service": "memos"}, nil
}

func (f *fakeUpstream) Add(context.Context, domain.AddPayload) (domain.AddResult, error) {
	return domain.AddResult{Status: "success"}, nil
}

// Search 固定返回一组混合记录，让 Dashboard 的本地兜底过滤与分页都有可断言的数据。
func (f *fakeUpstream) Search(_ context.Context, q domain.SearchQuery) (domain.SearchResult, error) {
	f.mu.Lock()
	f.searches = append(f.searches, q)
	f.mu.Unlock()
	fact := func(id, userID, nodeType, status string) domain.Memory {
		return domain.Memory{
			ID: id, Content: "alert " + id, Type: domain.MemoryTypeText,
			NodeType: nodeType, Status: status, UserID: userID,
		}
	}
	return domain.SearchResult{Facts: []domain.Memory{
		fact("s1", "alice", "LongTermMemory", "activated"),
		fact("s2", "alice", "LongTermMemory", "activated"),
		fact("s3", "alice", "LongTermMemory", "activated"),
		fact("s4", "bob", "LongTermMemory", "activated"),
		fact("s5", "alice", "LongTermMemory", "deleted"),
		fact("s6", "alice", "UserMemory", "activated"),
	}}, nil
}

func (f *fakeUpstream) lastSearch() domain.SearchQuery {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.searches) == 0 {
		return domain.SearchQuery{}
	}
	return f.searches[len(f.searches)-1]
}

func (f *fakeUpstream) Profile(context.Context, domain.ProfileQuery) (domain.ProfileResult, error) {
	return domain.ProfileResult{}, nil
}

func (f *fakeUpstream) OwnedMemory(_ context.Context, tenantID, _, id string) (domain.Memory, bool, error) {
	if tenantID != "cube-a" || id != "mem-1" {
		return domain.Memory{}, false, nil
	}
	return domain.Memory{ID: id, Content: "tea", UserID: "alice", Type: domain.MemoryTypeText}, true, nil
}

func (f *fakeUpstream) OwnedMemories(_ context.Context, tenantID, _ string, ids []string) (map[string]bool, error) {
	out := make(map[string]bool, len(ids))
	for _, id := range ids {
		if tenantID == "cube-a" && id == "mem-1" {
			out[id] = true
		}
	}
	return out, nil
}

func (f *fakeUpstream) Dashboard(_ context.Context, q domain.DashboardQuery) (domain.DashboardPage, error) {
	if q.TenantID != "cube-a" || q.Bucket != domain.DashboardBucketText || q.Filter["memory_type"] != "LongTermMemory" {
		return domain.DashboardPage{}, nil
	}
	return domain.DashboardPage{Total: 7, Rows: []domain.DashboardRow{{
		ID: "mem-1", Content: "tea", NodeType: "LongTermMemory", UserID: "alice",
		CreatedAt: "2026-10-18T23:00:31.581067000+00:00",
	}}}, nil
}

func (f *fakeUpstream) Owners(context.Context, []string) (map[string]string, error) {
	return map[string]string{}, nil
}

func (f *fakeUpstream) Delete(context.Context, string, []string) error { return nil }

func (f *fakeUpstream) Feedback(context.Context, domain.FeedbackRequest) (domain.FeedbackResult, error) {
	return domain.FeedbackResult{}, nil
}

type fakeTemplates struct{ tenants []string }

func (f *fakeTemplates) ListProfileTemplates(_ context.Context, tenant string, _, _ int) (domain.ProfileTemplatePage, error) {
	f.tenants = append(f.tenants, tenant)
	return domain.ProfileTemplatePage{Total: 1, Items: []domain.ProfileTemplate{fakeTemplate()}}, nil
}

func (f *fakeTemplates) GetProfileTemplate(_ context.Context, tenant, id string) (domain.ProfileTemplate, error) {
	f.tenants = append(f.tenants, tenant)
	if id != "t1" {
		return domain.ProfileTemplate{}, domain.NotFound("upstream", "模板不存在")
	}
	return fakeTemplate(), nil
}

func (f *fakeTemplates) AddProfileTemplate(_ context.Context, tenant, _ string, _ map[string]any) (string, error) {
	f.tenants = append(f.tenants, tenant)
	return "t2", nil
}

func (f *fakeTemplates) UpdateProfileTemplate(_ context.Context, tenant, id string, _ domain.ProfileTemplateUpdate) (string, error) {
	f.tenants = append(f.tenants, tenant)
	return id, nil
}

func (f *fakeTemplates) DeleteProfileTemplate(_ context.Context, tenant, _ string) error {
	f.tenants = append(f.tenants, tenant)
	return nil
}

func fakeTemplate() domain.ProfileTemplate {
	return domain.ProfileTemplate{
		ID: "t1", Name: "画像", BoundCount: 2, UpdatedAt: "2026-10-21T08:00:00Z",
		Metadata: map[string]any{"基础": map[string]any{"年龄": "int"}, "templateId": "evil"},
	}
}

type fakePrompts struct {
	mu      sync.Mutex
	prompts map[string]string
}

func newFakePrompts() *fakePrompts { return &fakePrompts{prompts: map[string]string{}} }

const fakeDefaultPrompt = "内置规则 ${conversation}"

func (f *fakePrompts) GetExtractPrompt(_ context.Context, tenant string) (domain.ExtractPrompt, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	prompt, ok := f.prompts[tenant]
	if !ok {
		return domain.ExtractPrompt{
			Prompt: fakeDefaultPrompt, ExtractionTypes: []string{"text_mem"}, Source: "default", IsDefault: true,
		}, nil
	}
	return domain.ExtractPrompt{
		Prompt: prompt, ExtractionTypes: []string{"text_mem"}, UpdatedAt: "2026-10-22T00:00:00Z", Source: "saved",
	}, nil
}

func (f *fakePrompts) SaveExtractPrompt(_ context.Context, tenant, prompt string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.prompts[tenant] = prompt
	return nil
}

func (f *fakePrompts) ResetExtractPrompt(_ context.Context, tenant string) (domain.ExtractPrompt, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.prompts, tenant)
	return domain.ExtractPrompt{
		Prompt: fakeDefaultPrompt, ExtractionTypes: []string{"text_mem"}, Source: "default", IsDefault: true,
	}, nil
}

func (f *fakePrompts) GenerateExtractPrompt(_ context.Context, _, description string) (string, error) {
	return "重写(" + description + ") ${conversation}", nil
}

func (f *fakePrompts) DebugExtract(
	_ context.Context, _ string, messages []domain.PromptMessage, types []string,
) (domain.ExtractDebug, error) {
	if len(types) == 0 {
		types = []string{"text_mem"}
	}
	content := ""
	if len(messages) > 0 {
		content = messages[0].Content
	}
	return domain.ExtractDebug{
		ExtractionTypes: types,
		Results:         []domain.ExtractDebugResult{{Type: "LongTermMemory", Content: content}},
	}, nil
}

type fakeModels struct{}

func (fakeModels) Models(context.Context) ([]domain.ModelInfo, error) {
	return []domain.ModelInfo{
		{Role: "记忆读取", Model: "reader", Spec: "host", Source: "MEMRADER_MODEL", Group: "memory"},
		{Role: "对话", Model: "chat", Source: "MOS_CHAT_MODEL", Group: "chat"},
	}, nil
}

type fakeGraph struct {
	memoryBody domain.GraphMemory
}

func (f *fakeGraph) Ping(context.Context) error { return nil }

func (f *fakeGraph) EventMemories(_ context.Context, _ domain.EventMemoriesQuery) ([]domain.GraphMemory, int, error) {
	return []domain.GraphMemory{{
		ID: "ctx-1", Content: "digest", Type: "Context", UserID: "cube-a", CreatedAt: "2026-10-18T09:10:38.097378Z",
	}}, 1, nil
}

func (f *fakeGraph) GraphMemories(context.Context, string, []string, int) (domain.GraphPage, error) {
	return domain.GraphPage{
		Nodes: []domain.GraphMemory{{ID: "m1", Content: "body", Type: "Context", UserID: "cube-a"}},
	}, nil
}

func (f *fakeGraph) MemoryBody(context.Context, string, string) (domain.GraphMemory, error) {
	return f.memoryBody, nil
}

func TestMemoryListClockAndShape(t *testing.T) {
	srv := newTestServer(t)
	text := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/list",
		`{"projectId":"cube-a","memoryDetailType":"text_mem","pageNum":1,"pageSize":20}`)
	for _, want := range []string{
		`"total":7`, `"current":1`, `"createTime":"2026-10-19 07:00:31"`, `"subjectType":"user"`,
	} {
		if !strings.Contains(text.Body.String(), want) {
			t.Fatalf("text_mem 缺少 %s: %s", want, text.Body.String())
		}
	}
	if strings.Contains(text.Body.String(), "leak-key") {
		t.Fatalf("技能名混进了事实记忆: %s", text.Body.String())
	}
	event := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/list",
		`{"projectId":"cube-a","memoryDetailType":"event_mem"}`)
	for _, want := range []string{`"createTime":"2026-10-18 17:10:38"`, `"subjectType":"agent"`, `"memoryType":"Context"`} {
		if !strings.Contains(event.Body.String(), want) {
			t.Fatalf("event_mem 缺少 %s: %s", want, event.Body.String())
		}
	}
	kb := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/list", `{"projectId":"cube-a","knowledgeBaseId":"kb"}`)
	if kb.Code != http.StatusServiceUnavailable || !strings.Contains(kb.Body.String(), `"code":50300`) {
		t.Fatalf("知识库未启用应返回 503 信封，而不是空列表: %d %s", kb.Code, kb.Body.String())
	}
}

// 检索页签把 status/memory_type/user_id 下推上游，并在本地按同一条件兜底过滤、分页。
func TestMemorySearchPushesFilterAndPaginates(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	fake, ok := srv.deps.Memories.(*fakeUpstream)
	if !ok {
		t.Fatal("测试服务器应装配 fakeUpstream")
	}
	cases := []struct {
		name      string
		body      string
		wantIDs   []string
		wantTotal int
		wantUser  string
	}{
		{
			name:      "第一页",
			body:      `{"projectId":"cube-a","memoryDetailType":"text_mem","memSearchContent":"alert","pageNum":1,"pageSize":2}`,
			wantIDs:   []string{"s1", "s2"},
			wantTotal: 4,
		},
		{
			name:      "第二页",
			body:      `{"projectId":"cube-a","memoryDetailType":"text_mem","memSearchContent":"alert","pageNum":2,"pageSize":2}`,
			wantIDs:   []string{"s3", "s4"},
			wantTotal: 4,
		},
		{
			name:      "超出页返回空但 total 不变",
			body:      `{"projectId":"cube-a","memoryDetailType":"text_mem","memSearchContent":"alert","pageNum":3,"pageSize":2}`,
			wantIDs:   nil,
			wantTotal: 4,
		},
		{
			name:      "按主体过滤",
			body:      `{"projectId":"cube-a","memoryDetailType":"text_mem","memSearchContent":"alert","externalUserId":"bob","pageNum":1,"pageSize":10}`,
			wantIDs:   []string{"s4"},
			wantTotal: 1,
			wantUser:  "bob",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// 不并行：断言 lastSearch，需要与本次请求一一对应。
			rec := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/list", tc.body)
			if rec.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
			}
			var env struct {
				Data struct {
					Items []struct {
						MemoryID string `json:"memoryId"`
					} `json:"memListItemResponseList"`
					Total   int `json:"total"`
					Current int `json:"current"`
				} `json:"data"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if env.Data.Total != tc.wantTotal {
				t.Fatalf("total=%d want=%d body=%s", env.Data.Total, tc.wantTotal, rec.Body.String())
			}
			gotIDs := make([]string, 0, len(env.Data.Items))
			for _, item := range env.Data.Items {
				gotIDs = append(gotIDs, item.MemoryID)
			}
			if strings.Join(gotIDs, ",") != strings.Join(tc.wantIDs, ",") {
				t.Fatalf("ids=%v want=%v", gotIDs, tc.wantIDs)
			}

			query := fake.lastSearch()
			if query.TopK != domain.ContractTopKMax {
				t.Fatalf("top_k=%d want=%d", query.TopK, domain.ContractTopKMax)
			}
			if query.Filter["status"] != "activated" || query.Filter["memory_type"] != "LongTermMemory" {
				t.Fatalf("filter 未下推: %v", query.Filter)
			}
			gotUser, hasUser := query.Filter["user_id"]
			if tc.wantUser == "" {
				if hasUser {
					t.Fatalf("未指定主体时不应下推 user_id: %v", query.Filter)
				}
			} else if gotUser != tc.wantUser {
				t.Fatalf("filter.user_id=%v want=%q", gotUser, tc.wantUser)
			}
		})
	}
}

// 事件记忆详情用同一次 GET 的 metadata.created_at 输出 createTime；上游缺字段时为空串。
func TestMemoryEventDetailCreateTime(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name        string
		createdAt   string
		userID      string
		wantCreate  string
		wantSubject string
		wantUser    string
	}{
		{
			name:        "带创建时间与主体",
			createdAt:   "2026-10-18T09:10:38.097378Z",
			userID:      "alice",
			wantCreate:  "2026-10-18 17:10:38",
			wantSubject: "user",
			wantUser:    "alice",
		},
		{
			name:        "缺创建时间与主体",
			createdAt:   "",
			userID:      "",
			wantCreate:  "",
			wantSubject: "agent",
			wantUser:    "cube-a",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			srv := newTestServer(t)
			srv.deps.Graph = &fakeGraph{memoryBody: domain.GraphMemory{
				ID: "e1", Content: "digest", Type: "Context", UserID: tc.userID, CreatedAt: tc.createdAt,
			}}
			rec := call(t, srv, http.MethodPost, "/api/dashboard/project/memory/detail",
				`{"projectId":"cube-a","memoryId":"e1","memType":"event_mem"}`)
			if rec.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
			}
			var env struct {
				Data map[string]any `json:"data"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if env.Data["memoryContent"] != "digest" {
				t.Fatalf("memoryContent=%v", env.Data["memoryContent"])
			}
			if env.Data["createTime"] != tc.wantCreate {
				t.Fatalf("createTime=%v want=%q", env.Data["createTime"], tc.wantCreate)
			}
			if env.Data["subjectType"] != tc.wantSubject || env.Data["externalUserId"] != tc.wantUser {
				t.Fatalf("subject=%v/%v want=%s/%s", env.Data["subjectType"], env.Data["externalUserId"], tc.wantSubject, tc.wantUser)
			}
		})
	}
}
