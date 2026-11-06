// Package service 是用例层，也是 MCP、REST、Dashboard 三种入站协议共用的业务实现（对应原 Python 网关的 MemoryService）。
// 它只依赖 domain，并按“使用方定义接口”的原则在本包内声明自己需要的端口（上游记忆服务、仓储、时钟等），由 adapter 实现。
package service
