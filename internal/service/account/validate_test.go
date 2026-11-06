package account

import (
	"strings"
	"testing"
)

func TestNormalizeUsername(t *testing.T) {
	t.Parallel()

	got, err := normalizeUsername("  Alice.Dev@Corp  ")
	if err != nil || got != "alice.dev@corp" {
		t.Fatalf("normalizeUsername = %q, %v", got, err)
	}
	for _, bad := range []string{"", "   ", "has space", "中文", strings.Repeat("a", 65), "semi;colon"} {
		if _, err := normalizeUsername(bad); err == nil {
			t.Fatalf("%q 应被拒绝", bad)
		}
	}
}

func TestValidatePassword(t *testing.T) {
	t.Parallel()

	for _, ok := range []string{"12345678", strings.Repeat("x", 128)} {
		if err := validatePassword(ok); err != nil {
			t.Fatalf("%d 字节应通过: %v", len(ok), err)
		}
	}
	for _, bad := range []string{"", "1234567", strings.Repeat("x", 129)} {
		if err := validatePassword(bad); err == nil {
			t.Fatalf("%d 字节应被拒绝", len(bad))
		}
	}
}
