package app

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func projectRecord(records []map[string]any, id string) map[string]any {
	for _, rec := range records {
		if rec["id"] == id {
			return rec
		}
	}
	return nil
}

func boolValue(v any) bool {
	b, _ := v.(bool)
	return b
}

// 停用项目不能是单向操作：owner 在项目页仍能看到它，并能自己重新启用。
func TestE2EDisabledProjectOwnerCanReEnable(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	const update = "/api/dashboard/project/update"

	carol := e.loginAs("carol")
	expect(t, "owner disables cube-a", carol.post(update, `{"id":"cube-a","status":"DISABLED"}`), http.StatusOK)

	list := carol.post("/api/dashboard/project/list", `{}`)
	rec := projectRecord(list.records(), "cube-a")
	if rec == nil || rec["role"] != "owner" || !boolValue(rec["disabled"]) {
		t.Fatalf("停用后 owner 应在项目列表看到 cube-a 且带 owner 角色: %s", list.raw)
	}
	if hasProject(carol.get("/api/dashboard/project/dict").list(), "cube-a") {
		t.Fatal("停用项目不应出现在侧栏项目选择器里")
	}

	alice := e.loginAs("alice")
	aliceList := alice.post("/api/dashboard/project/list", `{}`)
	aliceRec := projectRecord(aliceList.records(), "cube-a")
	if aliceRec == nil || aliceRec["role"] != "viewer" || !boolValue(aliceRec["disabled"]) {
		t.Fatalf("普通成员也应能看到停用项目及真实角色: %s", aliceList.raw)
	}
	expect(t, "viewer cannot enable", alice.post(update, `{"id":"cube-a","status":"ENABLED"}`), http.StatusForbidden)

	expect(t, "owner re-enables cube-a", carol.post(update, `{"id":"cube-a","status":"ENABLED"}`), http.StatusOK)
	list = carol.post("/api/dashboard/project/list", `{}`)
	rec = projectRecord(list.records(), "cube-a")
	if rec == nil || rec["role"] != "owner" || boolValue(rec["disabled"]) {
		t.Fatalf("重新启用后应恢复可用且保持 owner: %s", list.raw)
	}
	expect(t, "keys readable after enable", carol.post("/api/dashboard/keys/list", `{"projectId":"cube-a"}`), http.StatusOK)
	if enabledProject(carol.get("/api/dashboard/project/dict").list()) == "cube-a" {
		t.Fatal("启用项目不应顺带切换当前项目")
	}
}

// 个人空间可以加成员，被加入的成员必须能在项目列表里看到它；管理员非成员仍然看不到。
func TestE2EPersonalSpaceMemberVisible(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	personal := "u-" + ids["carol"]
	expect(t, "add bob to personal space",
		e.adminClient().post("/admin/tenants/"+personal+"/members", `{"username":"bob","role":"viewer"}`), http.StatusOK)

	bob := e.loginAs("bob")
	got := bob.post("/api/dashboard/project/list", `{}`).records()
	rec := projectRecord(got, personal)
	if rec == nil || rec["role"] != "viewer" || !boolValue(rec["member"]) {
		t.Fatalf("被加入的成员应看到个人空间及真实角色: %v", got)
	}
	expect(t, "member reads personal keys", bob.post("/api/dashboard/keys/list", `{"projectId":"`+personal+`"}`), http.StatusOK)

	carol := e.loginAs("carol")
	if projectRecord(carol.post("/api/dashboard/project/list", `{}`).records(), personal) == nil {
		t.Fatal("本人应看到自己的个人空间")
	}
	if projectRecord(e.loginAs("dave").post("/api/dashboard/project/list", `{}`).records(), personal) != nil {
		t.Fatal("非成员不应看到别人的个人空间")
	}
	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	if projectRecord(root.post("/api/dashboard/project/list", `{}`).records(), personal) != nil {
		t.Fatal("平台管理员非成员也不应看到别人的个人空间")
	}
}

// 归档 = 软删除 + 停用；upsert 只改元数据，不能把归档项目改回可用状态。
func TestE2EArchivedProjectRejectsEntryPoints(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	carol := e.loginAs("carol")

	invited := carol.post("/admin/tenants/cube-a/invites", `{"role":"viewer","expiresIn":"24h"}`)
	expect(t, "create invite before archive", invited, http.StatusOK)
	path := invited.str("path")
	if path == "" {
		t.Fatalf("创建邀请应返回一次性路径: %s", invited.raw)
	}

	expect(t, "archive cube-a", carol.post("/api/dashboard/project/delete/cube-a", ""), http.StatusOK)
	tenant, err := e.app.store.GetTenant(t.Context(), "cube-a")
	if err != nil || !tenant.Archived() || !tenant.Disabled {
		t.Fatalf("归档后应同时停用: %+v err=%v", tenant, err)
	}

	// 运维 upsert 不传 disabled（默认 false）也不能把归档项目改回可用。
	expect(t, "admin upsert archived project",
		e.adminClient().post("/admin/tenants", `{"id":"cube-a","user_id":"cube-a","name":"cube-a"}`), http.StatusOK)
	tenant, err = e.app.store.GetTenant(t.Context(), "cube-a")
	if err != nil || !tenant.Archived() || !tenant.Disabled {
		t.Fatalf("upsert 后归档项目仍必须保持停用: %+v err=%v", tenant, err)
	}

	expect(t, "create invite on archived",
		e.adminClient().post("/admin/tenants/cube-a/invites", `{"role":"viewer"}`), http.StatusForbidden)
	expect(t, "redeem invite on archived", e.loginAs("bob").post(path, ""), http.StatusForbidden)
	expect(t, "enable archived project",
		carol.post("/api/dashboard/project/update", `{"id":"cube-a","status":"ENABLED"}`), http.StatusConflict)

	p, err := e.app.accounts.Authenticate(t.Context(), domain.Credentials{Bearer: e2eAdminToken})
	if err != nil {
		t.Fatalf("authenticate admin token: %v", err)
	}
	err = e.app.accounts.SwitchProject(t.Context(), p, "cube-a")
	if !errors.Is(err, domain.ErrForbidden) || !strings.Contains(domain.SafeMessage(err), "归档") {
		t.Fatalf("归档项目不应能被切换为当前项目: %v", err)
	}

	restored := carol.post("/api/dashboard/project/restore/cube-a", "")
	expect(t, "restore archived project", restored, http.StatusOK)
	if restored.str("role") != "owner" || !boolValue(restored.obj()["member"]) {
		t.Fatalf("恢复后应返回真实 owner 角色: %s", restored.raw)
	}
}

// 建项目响应的 role/member 描述调用主体：账号建项目是 owner，管理 Token 不是成员。
func TestE2EProjectCreateReportsRealMembership(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)

	viaToken := e.adminClient().post("/api/dashboard/project/create", `{"name":"ops-acl","ownerUsername":"alice"}`)
	expect(t, "admin token creates project", viaToken, http.StatusOK)
	if viaToken.str("role") != "" || boolValue(viaToken.obj()["member"]) {
		t.Fatalf("管理 Token 不是项目成员，响应不应写成 owner: %s", viaToken.raw)
	}
	members := e.adminClient().get("/admin/tenants/ops-acl/members")
	if recs := members.records(); len(recs) != 1 || recs[0]["username"] != "alice" || recs[0]["role"] != "owner" {
		t.Fatalf("ownerUsername 指定的账号应是真正 owner: %s", members.raw)
	}

	viaAccount := e.loginAs("alice").post("/api/dashboard/project/create", `{"name":"alice-acl"}`)
	expect(t, "account creates project", viaAccount, http.StatusOK)
	if viaAccount.str("role") != "owner" || !boolValue(viaAccount.obj()["member"]) {
		t.Fatalf("账号创建者应返回 owner/member: %s", viaAccount.raw)
	}
}

// 全局密钥数与审计条数只给平台管理员，普通用户只拿数据源状态。
func TestE2EMetaGlobalCountsOnlyForPlatformAdmin(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)

	ordinary := e.loginAs("alice").get("/api/meta")
	expect(t, "ordinary meta", ordinary, http.StatusOK)
	sources, _ := ordinary.obj()["sources"].(map[string]any)
	if sources == nil {
		t.Fatalf("登录用户应拿到 sources: %s", ordinary.raw)
	}
	for _, key := range []string{"keys", "callLog"} {
		if _, ok := sources[key]; ok {
			t.Fatalf("普通用户不应看到全局 %s 计数: %s", key, ordinary.raw)
		}
	}

	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	rootMeta := root.get("/api/meta")
	rootSources, _ := rootMeta.obj()["sources"].(map[string]any)
	for _, key := range []string{"keys", "callLog"} {
		if _, ok := rootSources[key]; !ok {
			t.Fatalf("平台管理员应看到 %s 计数: %s", key, rootMeta.raw)
		}
	}
}
