package piagent

import "testing"

func TestForwardMessageUpdate(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		line string
		want bool
	}{
		{
			name: "text delta",
			line: `{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"hi"}}`,
			want: true,
		},
		{
			name: "thinking delta",
			line: `{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"考虑一下"}}`,
			want: true,
		},
		{
			name: "reasoning delta",
			line: `{"type":"message_update","assistantMessageEvent":{"type":"reasoning_delta","delta":"推理"}}`,
			want: true,
		},
		{
			// 工具参数增量只用于拼 JSON，最终调用由 tool_execution_* 表达。
			name: "toolcall delta",
			line: `{"type":"message_update","assistantMessageEvent":{"type":"toolcall_delta","delta":"{\"query\""}}`,
			want: false,
		},
		{
			name: "not an update",
			line: `{"type":"agent_start"}`,
			want: false,
		},
		{
			name: "broken json",
			line: `{`,
			want: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := forwardMessageUpdate([]byte(tc.line)); got != tc.want {
				t.Fatalf("forwardMessageUpdate(%s) = %v, want %v", tc.name, got, tc.want)
			}
		})
	}
}
