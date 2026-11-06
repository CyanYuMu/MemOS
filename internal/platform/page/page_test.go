package page_test

import (
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/page"
)

func TestSlice(t *testing.T) {
	t.Parallel()
	items := []int{1, 2, 3, 4, 5}
	cases := []struct {
		name       string
		page, size int
		wantLen    int
	}{
		{"first page", 1, 2, 2},
		{"last partial", 3, 2, 1},
		{"out of range", 4, 2, 0},
		{"negative page", -1, 2, 0},
		{"zero size", 1, 0, 0},
		{"negative size", 1, -1, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			if got := page.Slice(items, c.page, c.size); len(got) != c.wantLen {
				t.Fatalf("len=%d want %d", len(got), c.wantLen)
			}
		})
	}
}
