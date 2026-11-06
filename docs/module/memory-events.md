---
module: memory-events
packages:
  - internal/service/memory
  - internal/adapter/store
  - internal/transport/dashboard
status: implemented
---

# 记忆变化事件

聊天日志需要刷新后仍能按实际操作展示召回、新增、更新和删除，详情须保存操作当时的实体。已有审计是运维记录，正文有截断和清理；新增独立、不可变的业务事件，不回填历史，不把工具受理当作变化。

## 职责与边界

memory服务声明EventStore小端口；store实现SQLite追加与分页；app接线；dashboard只鉴权/编解码。domain定义MemoryEvent/Page。前端以服务器call_id关联其真实工具结果，不能靠时间或文本猜轮次。Pi子进程通过环境引用的X-GraphMemory-Chat-Session头带实际runtime sessionId，MCP将有效chat标识优先用于上游会话和事件关联；其他客户端仍使用原工具session_id/首消息规则。此头只是归档元数据，绝不作为项目或账号鉴权依据。

## 契约

- 轮次快照内联展开；召回实体的 Dashboard 投影保留该次实际检索相关度 `relativity`，可选数值。原始领域实体与不可变事件已经保存该字段，无需迁移存储；画像与旧记录缺少可用相关度时省略，前端显示未知，不从当前记忆或其他轮次补造。
- migration0029新增memory_events和独立sequence计数表。全局序号在同一写事务递增，不依赖SQLite隐式rowid，事件不可覆盖；唯一(tenant_id,event_key)保证同调用终态重复追加不改历史。无跨项目读取，无共享游标。
- 事件包括eventId、sequence、kind(recall/add/update/delete)、status(succeeded/failed/unavailable)、callId、sessionId、occurredAt、真实确认ids/total、memories、before、truncated。before仅更新/删除需要；更新no-op不制造变化事件。
- 回忆来自该次真实SearchResult；更新取已验证的前后实体；删除在归属检查通过后采集删除前实体，删除成功才记录。新增复用已实现的提取后快照；异步job包装保存可信call_id使重放保持关联。缺失旧call_id时由worker的可信job.ID恢复，校验队列与负载租户/用户一致；脱离worker而无ID的旧负载不猜造历史轮次。
- 实体ID、租户与用户显式冲突均拒绝进入投影。只补齐空作用域，保留七字段和版本；每组最多50实体，整体事件最多192KiB，超出省略完整实体并truncated，不截断字段伪造详情。确认ID投影最多50个且单ID最多256字节，省略时标记truncated；确认总数保持真实，，不能按当前库计数。
- 失败事件不带实体；新增异步永久失败由worker终态回调写入。读快照/事件落库失败不能重试已经成功的上游副作用；写库预算最多2秒并受调用方原期限约束，失败安全日志可追踪。此边界为best effort，不承诺上游与SQLite跨库事务或exactly-once。
- 新POST /api/dashboard/project/memory/events需项目查看权，body={projectId,after?}，records/cursor/hasMore。首次返回最近50条；后续按sequence升序取50条+1判断hasMore，保证相同时间戳不丢记录。游标为包含项目与序号的opaque编码，跨项目/非法游标拒绝。事件正文来自已持久化快照，不访问当前底座。
- MCP/REST成功响应新增可选call_id（沿用Add首次jobID），用于准确关联；仅可观测元数据，不作为权限依据。异步受理仍只代表pending，不能显示为新增完成。

## 兼容与回滚

已加载旧对话按真实 callIds（每批最多20个，与after互斥）读取持久化事件；查询始终限定当前项目，不查当前底座或回填旧值。新增表达式索引支持tenant+CallID查找，迁移只加索引，旧二进制可继续使用。定向结果不移动实时feed游标；前端保留当前已加载工具对应事件，最近80条窗口不能挤掉这些历史快照。不存在的事件才使用旧摘要兼容。

同一控制面实例对同租户/用户/记忆的显式更新以固定64个分片锁串行读写确认，避免本实例并发更新用过期before。正文更正读回版本必须恰好加1，否则不把旧读值作为可靠前态写入成功事件。上游没有条件写入契约，此处不承诺跨实例/外部写者的原子CAS。


## 测试与验证


## 依赖

使用方端口EventStore在memory服务声明，store结构化实现，app为唯一组合根。domain只依赖标准库；不新增第三方依赖。

## 错误与边界情况

上游失败只记录失败状态，不能编造实体。事件记录失败保留真实操作结果并安全记录kind/call_id，不触发上游重试。游标非法或项目不符返回Invalid，禁用端口返回Disabled；旧无关联数据不回填当前值。

## 参考来源

- internal/service/memory/activity.go、snapshot.go：已实现的新增审计快照及归属、预算边界。
- internal/worker/worker.go：原队列执行、终态回调和排空期限。
- 任务design.md及用户参考站截图：独立召回/新增/更新/删除卡片、逐行展开和稳定历史。

## 实现记录

See the public version-control history.
