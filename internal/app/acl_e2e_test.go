package app

import (
	"fmt"
	"net/http"
	"testing"
)

func (r result) hasRecord(key, value string) bool {
	for _, rec := range r.records() {
		if rec[key] == value {
			return true
		}
	}
	return false
}

func hasProject(records []map[string]any, id string) bool {
	for _, rec := range records {
		if rec["id"] == id {
			return true
		}
	}
	return false
}

// enabledProject 返回 dict 里唯一标记为当前的项目 ID。
func enabledProject(records []map[string]any) string {
	for _, rec := range records {
		if rec["status"] == "ENABLED" {
			return fmt.Sprint(rec["id"])
		}
	}
	return ""
}

func TestE2ERoleMatrix(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	const keyBody = `{"projectId":"cube-a","keyName":"ci"}`

	alice := e.loginAs("alice")
	dict := alice.get("/api/dashboard/project/dict")
	expect(t, "alice dict", dict, http.StatusOK)
	if got := dict.list(); len(got) != 2 || !hasProject(got, "cube-a") ||
		enabledProject(got) != "u-"+ids["alice"] {
		t.Fatalf("alice 应看到 cube-a 与自己的个人项目，且默认落在个人项目: %s", dict.raw)
	}
	for _, item := range dict.list() {
		if item["id"] == "cube-a" && item["role"] != "viewer" {
			t.Fatalf("cube-a 角色应为 viewer: %v", item)
		}
		if item["id"] == "u-"+ids["alice"] && item["role"] != "owner" {
			t.Fatalf("个人项目角色应为 owner: %v", item)
		}
	}
	expect(t, "viewer reads keys", alice.post("/api/dashboard/keys/list", `{"projectId":"cube-a"}`), http.StatusOK)
	expect(t, "viewer cannot create key", alice.post("/api/dashboard/keys/create", keyBody), http.StatusForbidden)
	expect(t, "non-member project", alice.post("/api/dashboard/keys/list", `{"projectId":"cube-b"}`), http.StatusForbidden)
	expect(t, "viewer cannot list members", alice.get("/admin/tenants/cube-a/members"), http.StatusForbidden)
	expect(t, "viewer cannot manage accounts", alice.get("/admin/accounts"), http.StatusForbidden)
	// 前端的 memory/delete 不带 projectId，应落到会话当前项目（cube-a）上判权。
	// 显式指定 cube-a：不带 projectId 会落到当前项目（个人空间，alice 在那里是 owner）。
	expect(t, "viewer cannot delete memory", alice.post("/api/dashboard/project/memory/delete", `{"projectId":"cube-a","memory_ids":["m1"],"memType":"text_mem"}`), http.StatusForbidden)
	expect(t, "viewer cannot save prompt", alice.post("/api/dashboard/prompt-config/save", `{"projectId":"cube-a","prompt":"p"}`), http.StatusForbidden)
	expect(t, "viewer cannot create profile template", alice.post("/api/dashboard/profileTemplate/create", `{"projectId":"cube-a","templateName":"t"}`), http.StatusForbidden)
	expect(t, "viewer cannot delete profile template", alice.post("/api/dashboard/profileTemplate/delete", `{"projectId":"cube-a","templateId":"t1"}`), http.StatusForbidden)
	expect(t, "non-member cannot read other project's templates", alice.post("/api/dashboard/profileTemplate/list", `{"projectId":"cube-b"}`), http.StatusForbidden)

	bob := e.loginAs("bob")
	created := bob.post("/api/dashboard/keys/create", keyBody)
	expect(t, "editor creates key", created, http.StatusOK)
	if got := created.str("createTime"); len(got) != 19 || got[10] != ' ' {
		t.Fatalf("Key 的 createTime 应是北京时间 YYYY-MM-DD HH:mm:ss: %q", got)
	}
	tok, err := e.app.store.GetToken(t.Context(), created.str("id"))
	if err != nil || tok.TenantID != "cube-a" || tok.CreatedByAccountID != ids["bob"] {
		t.Fatalf("Key 应属于 cube-a 并记录创建者: %+v err=%v", tok, err)
	}
	expect(t, "editor cannot add members", bob.post("/admin/tenants/cube-a/members", `{"username":"dave","role":"viewer"}`), http.StatusForbidden)
	expect(t, "editor cannot rename project", bob.post("/api/dashboard/project/update", `{"id":"cube-a","name":"renamed"}`), http.StatusForbidden)
	expect(t, "editor cannot edit project description", bob.post("/api/dashboard/project/update", `{"id":"cube-a","description":"x"}`), http.StatusForbidden)

	carol := e.loginAs("carol")
	meta := carol.post("/api/dashboard/project/update", `{"id":"cube-a","description":"安全项目","isMultiView":1}`)
	expect(t, "owner edits project meta", meta, http.StatusOK)
	if meta.str("description") != "安全项目" || fmt.Sprint(meta.obj()["isMultiView"]) != "1" {
		t.Fatalf("owner 修改后的项目元数据不对: %s", meta.raw)
	}
	members := carol.get("/admin/tenants/cube-a/members")
	expect(t, "owner lists members", members, http.StatusOK)
	if len(members.records()) != 3 {
		t.Fatalf("cube-a 应有 3 名成员: %s", members.raw)
	}
	expect(t, "owner promotes alice", carol.post("/admin/tenants/cube-a/members", `{"account_id":"`+ids["alice"]+`","role":"editor"}`), http.StatusOK)
	expect(t, "promoted viewer can create key", alice.post("/api/dashboard/keys/create", keyBody), http.StatusOK)
	expect(t, "owner removes bob", carol.post("/admin/tenants/cube-a/members/"+ids["bob"]+"/remove", ""), http.StatusOK)
	expect(t, "removed member loses access", bob.post("/api/dashboard/keys/create", keyBody), http.StatusForbidden)

	dave := e.loginAs("dave")
	if got := dave.get("/api/dashboard/project/dict").list(); len(got) != 1 || got[0]["kind"] != "personal" {
		t.Fatalf("无项目的账号应只看到自己的个人项目: %v", got)
	}
	expect(t, "dave non-member", dave.post("/api/dashboard/keys/list", `{"projectId":"cube-a"}`), http.StatusForbidden)

	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	if got := root.get("/api/dashboard/project/dict").list(); !hasProject(got, "cube-a") || !hasProject(got, "cube-b") {
		t.Fatalf("平台管理员应看到全部项目: %v", got)
	}
	expect(t, "platform admin writes anywhere", root.post("/api/dashboard/keys/create", `{"projectId":"cube-b","keyName":"ops"}`), http.StatusOK)
	expect(t, "platform admin manages accounts", root.get("/admin/accounts"), http.StatusOK)
}

func TestE2EProjectCreatorBecomesOwner(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)

	created := root.post("/api/dashboard/project/create", `{"name":"cube-owned","description":"creator owns it"}`)
	expect(t, "create project", created, http.StatusOK)

	members := root.get("/admin/tenants/cube-owned/members")
	expect(t, "list creator membership", members, http.StatusOK)
	records := members.records()
	if len(records) != 1 || records[0]["username"] != e2eSeedUser || records[0]["role"] != "owner" {
		t.Fatalf("创建者应自动成为 owner: %s", members.raw)
	}
}

func TestE2ELastOwnerIsProtected(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	carol := e.loginAs("carol")

	// cube-a 只有 carol 一个 owner：降级或移出自己都必须被拒绝。
	expect(t, "sole owner demotes self", carol.post("/admin/tenants/cube-a/members", `{"account_id":"`+ids["carol"]+`","role":"viewer"}`), http.StatusConflict)
	expect(t, "sole owner removes self", carol.post("/admin/tenants/cube-a/members/"+ids["carol"]+"/remove", ""), http.StatusConflict)
	expect(t, "owner still manages after rejected writes", carol.get("/admin/tenants/cube-a/members"), http.StatusOK)

	// 有第二个 owner 后 carol 仍不能自己退出（自移永远 409），要由另一个 owner 移出。
	expect(t, "promote bob", carol.post("/admin/tenants/cube-a/members", `{"account_id":"`+ids["bob"]+`","role":"owner"}`), http.StatusOK)
	expect(t, "owner still cannot leave self with another owner", carol.post("/admin/tenants/cube-a/members/"+ids["carol"]+"/remove", ""), http.StatusConflict)
	expect(t, "another owner removes carol", e.loginAs("bob").post("/admin/tenants/cube-a/members/"+ids["carol"]+"/remove", ""), http.StatusOK)
	expect(t, "removed owner loses access", carol.post("/api/dashboard/keys/list", `{"projectId":"cube-a"}`), http.StatusForbidden)
	if got := carol.get("/api/dashboard/project/dict").list(); hasProject(got, "cube-a") || enabledProject(got) != "u-"+ids["carol"] {
		t.Fatalf("退出 cube-a 后应回退到个人项目: %s", carol.get("/api/dashboard/project/dict").raw)
	}

	// bob 成为唯一 owner 后，平台管理员也不能把他移走；先补一个 owner 即可恢复。
	bob := e.loginAs("bob")
	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	expect(t, "platform admin removes sole owner", root.post("/admin/tenants/cube-a/members/"+ids["bob"]+"/remove", ""), http.StatusConflict)
	expect(t, "promote carol back", root.post("/admin/tenants/cube-a/members", `{"account_id":"`+ids["carol"]+`","role":"owner"}`), http.StatusOK)
	expect(t, "remove bob after recovery", root.post("/admin/tenants/cube-a/members/"+ids["bob"]+"/remove", ""), http.StatusOK)
	expect(t, "bob loses access", bob.post("/api/dashboard/keys/list", `{"projectId":"cube-a"}`), http.StatusForbidden)
}

func TestE2ECurrentProjectIsPerSession(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)

	s1, s2 := e.loginAs("carol"), e.loginAs("carol")
	enabled := func(c *e2eClient) string {
		t.Helper()
		var current []string
		for _, item := range c.get("/api/dashboard/project/dict").list() {
			if item["status"] == "ENABLED" {
				current = append(current, fmt.Sprint(item["id"]))
			}
		}
		if len(current) != 1 {
			t.Fatalf("同一时刻只应有一个 ENABLED 项目: %v", current)
		}
		return current[0]
	}
	if enabled(s1) != enabled(s2) || enabled(s1) == "cube-a" {
		t.Fatalf("未设置时两个会话都应默认落到个人项目: %s / %s", enabled(s1), enabled(s2))
	}
	personal := enabled(s1)
	expect(t, "switch s1", s1.post("/api/dashboard/project/update", `{"id":"cube-b","current":true}`), http.StatusOK)
	if enabled(s1) != "cube-b" {
		t.Fatal("s1 切换后当前项目应为 cube-b")
	}
	if enabled(s2) != personal {
		t.Fatal("切换项目只应影响本会话")
	}
	expect(t, "switch to non-member project", e.loginAs("alice").post("/api/dashboard/project/update", `{"id":"cube-b","current":true}`), http.StatusForbidden)

	expect(t, "key in a", s2.post("/api/dashboard/keys/create", `{"projectId":"cube-a","keyName":"only-a"}`), http.StatusOK)
	expect(t, "key in b", s1.post("/api/dashboard/keys/create", `{"projectId":"cube-b","keyName":"only-b"}`), http.StatusOK)
	listB := s1.post("/api/dashboard/keys/list", `{"projectId":"cube-b"}`)
	if !listB.hasRecord("keyName", "only-b") || listB.hasRecord("keyName", "only-a") {
		t.Fatalf("keys/list 只应包含所请求项目的 Key: %s", listB.raw)
	}
	listCurrent := s1.post("/api/dashboard/keys/list", `{}`)
	if !listCurrent.hasRecord("keyName", "only-b") || listCurrent.hasRecord("keyName", "only-a") {
		t.Fatalf("省略 projectId 时应取会话当前项目: %s", listCurrent.raw)
	}
}

func TestE2EKeyBelongsToOneProject(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	bob, carol := e.loginAs("bob"), e.loginAs("carol")

	other := carol.post("/api/dashboard/keys/create", `{"projectId":"cube-b","keyName":"b-key"}`)
	expect(t, "carol key in b", other, http.StatusOK)
	own := bob.post("/api/dashboard/keys/create", `{"projectId":"cube-a","keyName":"a-key"}`)
	expect(t, "bob key in a", own, http.StatusOK)

	expect(t, "revoke foreign key (dashboard)", bob.post("/api/dashboard/keys/revoke/"+other.str("id"), ""), http.StatusForbidden)
	expect(t, "revoke foreign key (admin)", bob.post("/admin/tokens/"+other.str("id")+"/revoke", ""), http.StatusForbidden)
	expect(t, "revoke unknown key id", bob.post("/api/dashboard/keys/revoke/no-such-key", ""), http.StatusForbidden)
	expect(t, "platform admin on unknown key id", e.adminClient().post("/api/dashboard/keys/revoke/no-such-key", ""), http.StatusNotFound)
	expect(t, "revoke own key", bob.post("/api/dashboard/keys/revoke/"+own.str("id"), ""), http.StatusOK)
	if bob.post("/api/dashboard/keys/list", `{"projectId":"cube-a"}`).hasRecord("id", own.str("id")) {
		t.Fatal("已吊销的 Key 不应再出现在控制台列表里")
	}
	expect(t, "admin token revokes any key", e.adminClient().post("/admin/tokens/"+other.str("id")+"/revoke", ""), http.StatusOK)

	viaToken := e.adminClient().post("/admin/tenants/cube-a/tokens", `{"label":"ops"}`)
	expect(t, "admin token issues key", viaToken, http.StatusOK)
	tok, err := e.app.store.GetToken(t.Context(), viaToken.str("id"))
	if err != nil || tok.CreatedByAccountID != "" {
		t.Fatalf("管理 Token 创建的 Key 创建者应为空: %+v err=%v", tok, err)
	}
}

func TestE2EProjectUpdateAuthorizesBeforeEffects(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	const update = "/api/dashboard/project/update"

	dave := e.loginAs("dave")
	expect(t, "non-member view", dave.post(update, `{"id":"cube-a"}`), http.StatusForbidden)
	expect(t, "non-member view of unknown id", dave.post(update, `{"id":"no-such"}`), http.StatusForbidden)

	bob := e.loginAs("bob")
	expect(t, "editor renames", bob.post(update, `{"id":"cube-a","name":"renamed"}`), http.StatusForbidden)
	expect(t, "editor disables", bob.post(update, `{"id":"cube-a","status":"DISABLED"}`), http.StatusForbidden)
	carol := e.loginAs("carol")
	expect(t, "unknown status", carol.post(update, `{"id":"cube-a","status":"enabled"}`), http.StatusBadRequest)
	expect(t, "non-string status", carol.post(update, `{"id":"cube-a","status":123}`), http.StatusBadRequest)

	expect(t, "bob joins cube-b", e.adminClient().post("/admin/tenants/cube-b/members", `{"username":"bob","role":"viewer"}`), http.StatusOK)
	expect(t, "denied rename must not switch", bob.post(update, `{"id":"cube-b","current":true,"name":"renamed"}`), http.StatusForbidden)
	for _, item := range bob.get("/api/dashboard/project/dict").list() {
		if item["id"] == "cube-b" && item["status"] == "ENABLED" {
			t.Fatal("授权失败的请求不应改动会话当前项目")
		}
	}
}
