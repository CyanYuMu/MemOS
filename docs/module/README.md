# 模块文档索引

每个功能模块先有文档，后有代码。规则见 [架构文档的“文档先行”](../ARCHITECTURE.md#文档先行)，新模块从 [_template.md](_template.md) 复制，文件名即模块名。`make docs-check` 校验：文档有完整章节、已列入下面的索引、代码所在的包被某个 `accepted` 或 `implemented` 的文档认领。

## 已有文档

| 模块 | 说明 |
|------|------|
| [bootstrap](bootstrap.md) | 进程入口、构建版本、角色启动、优雅停机 |
| [config](config.md) | 环境变量清单、解析与校验、脱敏摘要 |
| [logging](logging.md) | slog JSON、脱敏 handler、请求 ID |
| [platform](platform.md) | 共用的整数边界处理与内存分页 |
| [domain](domain.md) | 实体、值对象、typed errors、能力矩阵、时间口径 |
| [httpx](httpx.md) | 中间件、信封、请求体上限、鉴权上下文、健康检查 |
| [store](store.md) | SQLite 仓储、迁移、端口接口 |
| [identity](identity.md) | Token 哈希、Bearer 到租户、管理 Token |
| [zerotrust](zerotrust.md) | 零信任网关注入的 JWT 验签与外部身份 |
| [audit](audit.md) | 审计记录的异步落库与查询 |
| [upstream](upstream.md) | memos-api 客户端与上游怪癖屏蔽 |
| [memory-events](memory-events.md) | 不可变记忆事件与游标分页 |
| [memory](memory.md) | 5 个记忆用例 |
| [mcp](mcp.md) | `/mcp`、5 个工具、响应字段映射、golden |
| [rest](rest.md) | `/memory/*` REST 入口 |
| [worker](worker.md) | 异步写入任务 |
| [admin](admin.md) | 租户与 Token 管理（管理 Token） |
| [tenant](tenant.md) | 项目（租户）的创建、改名、启停、归档与运维 upsert 规则 |
| [jobs](jobs.md) | 异步写入任务的死信查询与人工重放（含重放安全判定） |
| [accounts](accounts.md) | 账号、项目空间成员、会话、登录与 ACL |
| [invites](invites.md) | 项目邀请链接：一次性 token、过期/撤销、落地页与兑换 |
| [redis](redis.md) | 登录节流的 Redis 共享计数（可选）与内存回退 |
| [dashboard-routes](dashboard-routes.md) | Dashboard 37 条路径的逐条状态与契约 |
| [skill-lifecycle](skill-lifecycle.md) | Skill 来源/工具观察、影响包、挑战与恢复控制面 |
| [prompt-studio](prompt-studio.md) | 提示词工作台的控制面代理：`prompts/*` 八条路由、权限、错误映射、限流与审计 |
| [observability](observability.md) | Prometheus 指标与可选告警规则（集群当前无采集端）、tracing 待决事项 |
| [deploy](deploy.md) | 镜像、dev 清单、生产发布/回滚与集群验证手册 |
| [chat](chat.md) | 管理后台对话页：前置校验、固定会话、SSE、128K 自动压缩 |
| [rust-pi-agent](rust-pi-agent.md) | pi 子进程运行时：spawn、JSONL 协议、BYOK、容量与回收 |
| [onboarding](onboarding.md) | 平台Web引导进度、账号/项目隔离与老用户兼容 |
| [local-sync](local-sync.md) | 本地插件记忆镜像：设备授权、分块上传、配额、转存 |

## 规划中的模块

当前没有已登记、尚未落地的模块。新增模块仍按依赖顺序先写文档再实现。


[Plugin artifact hosting](plugin.md)
