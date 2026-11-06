package dashboard

import (
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// source 过滤是“按入口出账”的基础：合法取值落到 Filter.Sources，
// 非法取值必须报参数错误而不是静默返回空列表。
func TestLogFilterSource(t *testing.T) {
	t.Parallel()

	base := map[string]any{"projectId": "cube-a"}

	f, err := logFilter(base, "cube-a", 1, 20)
	if err != nil {
		t.Fatalf("no source: %v", err)
	}
	if len(f.Sources) != 0 {
		t.Fatalf("未指定来源时不应过滤: %v", f.Sources)
	}

	withSource := map[string]any{"projectId": "cube-a", "source": domain.AuditSourceREST}
	f, err = logFilter(withSource, "cube-a", 1, 20)
	if err != nil {
		t.Fatalf("source=rest: %v", err)
	}
	if len(f.Sources) != 1 || f.Sources[0] != domain.AuditSourceREST {
		t.Fatalf("Sources = %v", f.Sources)
	}
	// 按来源筛不改变事件类型的语义：两者可以叠加。
	withBoth := map[string]any{"projectId": "cube-a", "source": domain.AuditSourceREST, "eventType": "addMessage"}
	f, err = logFilter(withBoth, "cube-a", 1, 20)
	if err != nil {
		t.Fatalf("source+eventType: %v", err)
	}
	if len(f.Sources) != 1 || len(f.Tools) != 1 || f.Tools[0] != domain.ToolAddMessage {
		t.Fatalf("Sources=%v Tools=%v", f.Sources, f.Tools)
	}

	if _, err := logFilter(map[string]any{"projectId": "cube-a", "source": "grpc"}, "cube-a", 1, 20); err == nil {
		t.Fatal("非法来源应报错")
	}
}
