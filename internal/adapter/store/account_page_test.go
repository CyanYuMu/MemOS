package store_test

import (
	"context"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ListAccountsPage 把过滤与分页下推到 SQL：账号超过一页时，过滤后总数、当页数据与
// created_at ASC, id ASC 的排序都必须保持旧语义。
func TestListAccountsPage(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 22, 16, 0, 0, 0, time.UTC)
	seed := func(id, username, displayName string, createdAt time.Time) {
		t.Helper()
		if err := s.CreateAccount(ctx, domain.Account{
			ID: id, Username: username, DisplayName: displayName,
			PasswordHash: "hash", CreatedAt: createdAt,
		}); err != nil {
			t.Fatalf("CreateAccount %s: %v", id, err)
		}
	}
	// alice 与 bob 同一秒创建，用 id 决定先后。
	seed("acct-alice", "alice", "Alice Zhang", base)
	seed("acct-bob", "bob", "Bob Li", base)
	seed("acct-carol", "carol", "Carol", base.Add(time.Minute))
	seed("acct-dave", "dave", "David", base.Add(2*time.Minute))
	seed("acct-erin", "erin", "Erin", base.Add(3*time.Minute))

	cases := []struct {
		name      string
		query     string
		limit     int
		offset    int
		wantTotal int
		wantIDs   []string
	}{
		{"空查询第一页", "", 2, 0, 5, []string{"acct-alice", "acct-bob"}},
		{"空查询第二页", "", 2, 2, 5, []string{"acct-carol", "acct-dave"}},
		{"空查询最后一页", "", 2, 4, 5, []string{"acct-erin"}},
		{"用户名大小写不敏感", "ALI", 10, 0, 1, []string{"acct-alice"}},
		{"显示名子串", "zhang", 10, 0, 1, []string{"acct-alice"}},
		{"没有匹配", "nobody", 10, 0, 0, nil},
		{"通配符按字面处理", "%", 10, 0, 0, nil},
		{"下划线按字面处理", "_", 10, 0, 0, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got, total, err := s.ListAccountsPage(ctx, tc.query, tc.limit, tc.offset)
			if err != nil {
				t.Fatalf("ListAccountsPage: %v", err)
			}
			if total != tc.wantTotal {
				t.Fatalf("total = %d, want %d", total, tc.wantTotal)
			}
			ids := make([]string, 0, len(got))
			for i := range got {
				ids = append(ids, got[i].ID)
			}
			if len(ids) != len(tc.wantIDs) {
				t.Fatalf("ids = %v, want %v", ids, tc.wantIDs)
			}
			for i := range ids {
				if ids[i] != tc.wantIDs[i] {
					t.Fatalf("ids = %v, want %v", ids, tc.wantIDs)
				}
			}
		})
	}
}
