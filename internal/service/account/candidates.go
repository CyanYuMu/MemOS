package account

import (
	"context"
	"errors"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// memberCandidateLimit 是成员候选的最大条数；接口不提供全量账号目录。
const memberCandidateLimit = 10

// minCandidateQuery 是候选搜索的最短查询长度（按字符数）。
// 单字符查询会命中大量账号，等于给任意项目 owner 一个账号枚举入口。
const minCandidateQuery = 2

// MemberCandidates 搜索可加入项目的账号：用户名前缀 + 显示名子串，最多 10 条。
// 空查询返回空列表；单字符只做用户名精确匹配；结果带 Member 标记，前端据此禁用已加入的人。
func (s *Service) MemberCandidates(
	ctx context.Context,
	actor domain.Principal,
	tenantID,
	query string,
) ([]domain.MemberCandidate, error) {
	if err := actor.RequireCap(tenantID, domain.CapProjectManage); err != nil {
		return nil, err
	}
	query = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(query), "@"))
	var accounts []domain.Account
	switch n := len([]rune(query)); {
	case n == 0:
		return nil, nil
	case n < minCandidateQuery:
		// 单字符只做用户名精确匹配：允许 1 个字符的合法用户名被加进来，
		// 又不给“逐字符枚举”留入口。
		acct, err := s.store.AccountByUsername(ctx, strings.ToLower(query))
		if errors.Is(err, domain.ErrNotFound) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		accounts = []domain.Account{acct}
	default:
		var err error
		accounts, err = s.store.SearchAccounts(ctx, query, memberCandidateLimit)
		if err != nil {
			return nil, err
		}
	}
	members, err := s.store.ListMembers(ctx, tenantID)
	if err != nil {
		return nil, err
	}
	present := make(map[string]struct{}, len(members))
	for i := range members {
		present[members[i].AccountID] = struct{}{}
	}
	out := make([]domain.MemberCandidate, 0, len(accounts))
	for i := range accounts {
		_, member := present[accounts[i].ID]
		out = append(out, domain.MemberCandidate{
			AccountID:   accounts[i].ID,
			Username:    accounts[i].Username,
			DisplayName: accounts[i].DisplayName,
			Disabled:    accounts[i].Disabled,
			Member:      member,
		})
	}
	return out, nil
}
