// Package piagent 以子进程方式驱动 rust-pi 的 RPC 模式：每个会话一个
// `pi --mode rpc`，stdio 走 JSONL，事件按订阅者广播给 transport/chat。
package piagent
