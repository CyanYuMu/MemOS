package store_test

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestGeneratedTitleIsOwnerScopedAndWriteOnce(t *testing.T) {
	t.Parallel()
	s := openStore(t)
	ctx := t.Context()
	c := domain.ChatConversation{ID: "c", TenantID: "p", AccountID: "a", CreatedAt: time.Now()}
	if err := s.CreateChatConversation(ctx, c); err != nil {
		t.Fatal(err)
	}
	for _, scope := range [][2]string{{"foreign", "a"}, {"p", "other"}} {
		ok, err := s.SetChatConversationTitleIfEmpty(ctx, scope[0], scope[1], "c", "leak")
		if err != nil || ok {
			t.Fatalf("foreign update: %v %v", ok, err)
		}
	}
	ok, err := s.SetChatConversationTitleIfEmpty(ctx, "p", "a", "c", "午餐川菜建议")
	if err != nil || !ok {
		t.Fatalf("first: %v %v", ok, err)
	}
	ok, err = s.SetChatConversationTitleIfEmpty(ctx, "p", "a", "c", "覆盖")
	if err != nil || ok {
		t.Fatalf("overwrite: %v %v", ok, err)
	}
	got, err := s.GetChatConversation(ctx, "p", "a", "c")
	if err != nil || got.Title != "午餐川菜建议" {
		t.Fatalf("read: %+v %v", got, err)
	}
}

func TestFeedbackCommentPersistsWithoutModelAndKeepsVoteUpdates(t *testing.T) {
	t.Parallel()
	s := openStore(t)
	ctx := t.Context()
	comment := "请更简洁"
	record := domain.ChatAnswerFeedback{Vote: -1, Comment: comment, ConversationID: "c", AnswerExcerpt: "原回答"}
	if err := s.SaveAnswerFeedback(ctx, "p", "a", "r", record, true, time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := s.SaveChatFeedback(ctx, "p", "a", "r", 1, time.Now()); err != nil {
		t.Fatal(err)
	}
	got, err := s.AnswerFeedback(ctx, "p", "a", "r")
	if err != nil || got.Comment != comment || got.Vote != 1 || got.ConversationID != "c" || got.AnswerExcerpt != "原回答" {
		t.Fatalf("read: %+v %v", got, err)
	}
	other, err := s.AnswerFeedback(ctx, "p", "other", "r")
	if err != nil || other.Comment != "" || other.Vote != 0 {
		t.Fatalf("leak: %+v %v", other, err)
	}
	if err := s.SaveChatFeedback(ctx, "p", "a", "r", 0, time.Now()); err != nil {
		t.Fatal(err)
	}
	got, err = s.AnswerFeedback(ctx, "p", "a", "r")
	if err != nil || got.Comment != "" || got.Vote != 0 {
		t.Fatalf("cancel: %+v %v", got, err)
	}
}

func TestFeedbackSurvivesStoreReopen(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	path := filepath.Join(t.TempDir(), "feedback.db")
	s, err := store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	f := domain.ChatAnswerFeedback{Vote: -1, Comment: "请给可执行的例子", ConversationID: "room", AnswerExcerpt: "回答"}
	if err := s.SaveAnswerFeedback(ctx, "p", "a", "r", f, true, time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	restored, err := store.Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	got, err := restored.AnswerFeedback(ctx, "p", "a", "r")
	if err != nil || got.Comment != f.Comment || got.ConversationID != f.ConversationID || got.Vote != -1 {
		t.Fatalf("durability: %+v %v", got, err)
	}
}
