// Package mcp 是 /mcp（Streamable HTTP）的协议适配。
// 5 个工具的名称、参数、描述是对外契约（描述本身就是 prompt），用 test/golden 里的快照锁定。
// 身份只来自 Bearer token，绝不接受工具参数里的 user_id。
package mcp
