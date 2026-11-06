package domain

import "testing"

func TestTruncateRunesKeepsUTF8(t *testing.T) {
	t.Parallel()

	cases := []struct {
		in    string
		limit int
		want  string
	}{
		{"", 5, ""},
		{"abcdef", 0, ""},
		{"abcdef", 10, "abcdef"},
		{"用户名超过上限", 3, "用户名"},
		{"账号", 3, "账号"},
	}
	for _, c := range cases {
		if got := TruncateRunes(c.in, c.limit); got != c.want {
			t.Errorf("TruncateRunes(%q, %d) = %q, want %q", c.in, c.limit, got, c.want)
		}
	}
}
