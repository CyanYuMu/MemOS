package app

import (
	"database/sql"
	"errors"
	"net/http"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// execAppRaw 在 e2e 的真实 SQLite 库上执行一条 SQL（建/删触发器）。
func execAppRaw(t *testing.T, e *e2eEnv, stmt string) {
	t.Helper()
	db, err := sql.Open("sqlite", "file:"+e.app.cfg.Store.Path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}
	defer func() { _ = db.Close() }()
	if _, err := db.ExecContext(t.Context(), stmt); err != nil {
		t.Fatalf("exec %q: %v", stmt, err)
	}
}

// TestE2EProjectCreateRollsBackOnOwnerFailure 证明租户行与 owner 成员行同事务：
// 成员写入失败时接口报错，库里不残留无 owner 项目。
func TestE2EProjectCreateRollsBackOnOwnerFailure(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	execAppRaw(t, e, `CREATE TRIGGER fail_project_owner BEFORE INSERT ON project_members
		WHEN NEW.tenant_id = 'broken-team'
		BEGIN SELECT RAISE(ABORT, 'injected owner failure'); END`)

	alice := e.loginAs("alice")
	got := alice.post("/api/dashboard/project/create", `{"name":"broken-team"}`)
	if got.status != http.StatusInternalServerError || got.code != 50000 {
		t.Fatalf("创建失败应返回 500 信封，得到 %d %s", got.status, got.raw)
	}
	if _, err := e.app.store.GetTenant(t.Context(), "broken-team"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("回滚后不应残留项目行: %v", err)
	}

	// 去掉触发器后可正常创建，证明失败没有污染连接或后续写入。
	execAppRaw(t, e, `DROP TRIGGER fail_project_owner`)
	expect(t, "create after rollback", alice.post("/api/dashboard/project/create", `{"name":"broken-team"}`), http.StatusOK)
	members := alice.get("/admin/tenants/broken-team/members")
	expect(t, "owner after retry", members, http.StatusOK)
	if records := members.records(); len(records) != 1 || records[0]["username"] != "alice" || records[0]["role"] != "owner" {
		t.Fatalf("重试后创建者应是 owner: %s", members.raw)
	}
}

// TestE2EDisableRollsBackWhenSessionRevokeFails 证明禁用账号与吊销会话同事务：
// 吊销失败时账号仍启用、旧会话仍有效；恢复后重试成功，旧会话才失效。
func TestE2EDisableRollsBackWhenSessionRevokeFails(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ids := seedWorld(t, e)
	admin := e.adminClient()
	alice := e.loginAs("alice")
	expect(t, "alice session before", alice.get("/api/auth/me"), http.StatusOK)

	execAppRaw(t, e, `CREATE TRIGGER fail_alice_revoke BEFORE UPDATE ON sessions
		WHEN OLD.account_id = '`+ids["alice"]+`'
		BEGIN SELECT RAISE(ABORT, 'injected revoke failure'); END`)

	got := admin.post("/admin/accounts/"+ids["alice"], `{"disabled":true}`)
	if got.status != http.StatusInternalServerError || got.code != 50000 {
		t.Fatalf("禁用失败应返回 500 信封，得到 %d %s", got.status, got.raw)
	}
	detail := admin.get("/admin/accounts/" + ids["alice"])
	expect(t, "account detail after rollback", detail, http.StatusOK)
	if disabled, _ := detail.obj()["disabled"].(bool); disabled {
		t.Fatalf("回滚后账号应保持启用: %s", detail.raw)
	}
	expect(t, "old session survives rollback", alice.get("/api/auth/me"), http.StatusOK)

	execAppRaw(t, e, `DROP TRIGGER fail_alice_revoke`)
	expect(t, "disable after rollback", admin.post("/admin/accounts/"+ids["alice"], `{"disabled":true}`), http.StatusOK)
	expect(t, "old session revoked after retry", alice.get("/api/auth/me"), http.StatusUnauthorized)
}
