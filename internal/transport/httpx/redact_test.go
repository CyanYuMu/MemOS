package httpx

import "testing"

func TestRedactPath(t *testing.T) {
	t.Parallel()

	cases := map[string]string{
		"/__invite/inv_0123456789abcdef": "/__invite/***",
		"/__invite/":                     "/__invite/",
		"/api/dashboard/project/dict":    "/api/dashboard/project/dict",
		"/__login":                       "/__login",
	}
	for in, want := range cases {
		if got := redactPath(in); got != want {
			t.Errorf("redactPath(%q) = %q, want %q", in, got, want)
		}
	}
}
