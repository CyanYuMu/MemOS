package onboarding

import (
	"context"
	"slices"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Store 按账号和项目保存检查点，写入必须校验账号级revision。
type Store interface {
	GetOnboarding(context.Context, string, string) (domain.OnboardingProgress, error)
	SaveOnboarding(context.Context, string, string, domain.OnboardingProgress) error
	GetChatConversation(context.Context, string, string, string) (domain.ChatConversation, error)
}

type Service struct{ store Store }

func New(store Store) *Service { return &Service{store: store} }

func authorize(p domain.Principal, project string) error {
	if p.Kind != domain.PrincipalAccount || p.AccountID == "" {
		return domain.Invalid("onboarding", "请使用控制台账号登录")
	}
	if project == "" {
		return domain.Invalid("onboarding", "请选择空间")
	}
	return p.RequireCap(project, domain.CapProjectView)
}

func (s *Service) Get(ctx context.Context, p domain.Principal, project string) (domain.OnboardingProgress, error) {
	if err := authorize(p, project); err != nil {
		return domain.OnboardingProgress{}, err
	}
	return s.store.GetOnboarding(ctx, p.AccountID, project)
}

func (s *Service) Update(ctx context.Context, p domain.Principal, project, action string, revision int64, checkpoint ...domain.OnboardingCheckpoint) (domain.OnboardingProgress, error) {
	state, err := s.Get(ctx, p, project)
	if err != nil {
		return state, err
	}
	if state.Revision != revision {
		return state, domain.Wrap(domain.ErrConflict, "onboarding", "进度已更新，请重试", nil)
	}
	switch action {
	case "start":
		state.Status = "active"
	case "replay":
		state.Status, state.Step = "active", 0
		state.Chat = nil
	case "dismiss":
		state.Status = "dismissed"
	case "advance":
		if state.Status != "active" || state.Step >= 6 {
			return state, domain.Invalid("onboarding", "请先开始指引")
		}
		state.Step++
		if state.Step == 6 {
			state.Status = "completed"
		}
	case "back":
		if state.Status != "active" || state.Step <= 0 || state.Step > 5 {
			return state, domain.Invalid("onboarding", "当前步骤无法返回")
		}
		state.Step--
	case "checkpoint":
		if len(checkpoint) != 1 || state.Status != "active" || state.Step != 3 {
			return state, domain.Invalid("onboarding", "请先进入对话指引")
		}
		if err := s.validateCheckpoint(ctx, p, project, checkpoint[0]); err != nil {
			return state, err
		}
		state.Chat = &checkpoint[0]
	default:
		return state, domain.Invalid("onboarding", "操作无效")
	}
	if err := s.store.SaveOnboarding(ctx, p.AccountID, project, state); err != nil {
		return state, err
	}
	state.Revision++
	return state, nil
}

func (s *Service) validateCheckpoint(ctx context.Context, p domain.Principal, project string, c domain.OnboardingCheckpoint) error {
	if !slices.Contains([]string{"first", "logs", "recall", "finish"}, c.Stage) || c.First == "" || len(c.First) > 80 || len(c.Second) > 80 {
		return domain.Invalid("onboarding", "对话进度无效")
	}
	if (c.Stage == "recall" || c.Stage == "finish") && (c.Second == "" || c.Second == c.First) {
		return domain.Invalid("onboarding", "请选择另一段对话")
	}
	for _, id := range []string{c.First, c.Second} {
		if id == "" {
			continue
		}
		if _, err := s.store.GetChatConversation(ctx, project, p.AccountID, id); err != nil {
			return domain.Wrap(domain.ErrForbidden, "onboarding", "无法恢复该对话", nil)
		}
	}
	return nil
}
