package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestRun(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name     string
		args     []string
		wantCode int
		wantOut  string
		wantErr  string
	}{
		{name: "version", args: []string{"version"}, wantCode: 0, wantOut: "dev"},
		{name: "no args", args: nil, wantCode: 2, wantErr: "usage:"},
		{name: "unknown command", args: []string{"bogus"}, wantCode: 2, wantErr: "usage:"},
		{name: "chown no dir", args: []string{"chown"}, wantCode: 2, wantErr: "usage:"},
		{name: "serve bad role", args: []string{"serve", "--role=bogus"}, wantCode: 2, wantErr: "role"},
		{name: "serve unknown flag", args: []string{"serve", "--nope"}, wantCode: 2, wantErr: "flag"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()

			var stdout, stderr bytes.Buffer
			if got := run(tt.args, &stdout, &stderr); got != tt.wantCode {
				t.Fatalf("exit code = %d, want %d (stderr=%q)", got, tt.wantCode, stderr.String())
			}
			if !strings.Contains(stdout.String(), tt.wantOut) {
				t.Errorf("stdout = %q, want substring %q", stdout.String(), tt.wantOut)
			}
			if !strings.Contains(strings.ToLower(stderr.String()), tt.wantErr) {
				t.Errorf("stderr = %q, want substring %q", stderr.String(), tt.wantErr)
			}
		})
	}
}
