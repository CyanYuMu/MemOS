// Package rest 是 /memory/* 兼容入口，给进程内、拿不到 MCP 上下文的调用方使用（如 Hermes memory provider）。
// 与 mcp 共用同一套鉴权、租户与审计；add 也是异步入队，两者只在响应形状上不同。
package rest
