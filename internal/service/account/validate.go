package account

import (
	"regexp"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	minPasswordBytes = 8
	maxPasswordBytes = 128
)

var usernamePattern = regexp.MustCompile(`^[a-z0-9._@-]{1,64}$`)

// normalizeUsername 先 TrimSpace 再转小写，只允许 [a-z0-9._@-]{1,64}。
func normalizeUsername(raw string) (string, error) {
	name := strings.ToLower(strings.TrimSpace(raw))
	if !usernamePattern.MatchString(name) {
		return "", domain.Invalid("account", "用户名只能包含小写字母、数字和 . _ @ -，长度 1 到 64")
	}
	return name, nil
}

func validatePassword(password string) error {
	if len(password) < minPasswordBytes || len(password) > maxPasswordBytes {
		return domain.Invalid("account", "口令长度必须在 8 到 128 字节之间")
	}
	return nil
}
