package num_test

import (
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
)

func TestClamp(t *testing.T) {
	t.Parallel()
	cases := []struct{ v, lo, hi, want int }{
		{5, 1, 10, 5},
		{0, 1, 10, 1},
		{11, 1, 10, 10},
		{-9, 1, 10, 1},
	}
	for _, c := range cases {
		if got := num.Clamp(c.v, c.lo, c.hi); got != c.want {
			t.Errorf("Clamp(%d,%d,%d)=%d want %d", c.v, c.lo, c.hi, got, c.want)
		}
	}
}

func TestWithDefault(t *testing.T) {
	t.Parallel()
	if got := num.WithDefault(0, 20, 1, 200); got != 20 {
		t.Errorf("zero should take default, got %d", got)
	}
	if got := num.WithDefault(500, 20, 1, 200); got != 200 {
		t.Errorf("default should still clamp, got %d", got)
	}
	if got := num.WithDefault(3, 20, 1, 200); got != 3 {
		t.Errorf("explicit value should win, got %d", got)
	}
}
