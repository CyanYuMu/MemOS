package store_test

import (
	"context"
	"testing"
	"time"
)

func TestChatFeedbackIsolationAndCancellation(t *testing.T) {
	t.Parallel()
	s := openStore(t)
	ctx := context.Background()
	for _, vote := range []int{1, -1, 0} {
		if err := s.SaveChatFeedback(ctx, "p1", "alice", "answer", vote, time.Now()); err != nil {
			t.Fatal(err)
		}
		got, err := s.ChatFeedback(ctx, "p1", "alice", "answer")
		if err != nil || got != vote {
			t.Fatalf("vote=%d err=%v", got, err)
		}
		for _, scope := range [][3]string{{"p2", "alice", "answer"}, {"p1", "bob", "answer"}, {"p1", "alice", "different"}} {
			other, e := s.ChatFeedback(ctx, scope[0], scope[1], scope[2])
			if e != nil || other != 0 {
				t.Fatalf("leaked feedback: %d %v", other, e)
			}
		}
	}
}
