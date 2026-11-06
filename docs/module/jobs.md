---
module: jobs
packages:
  - internal/service/jobs
status: implemented
---

# jobs

异步写入任务的运维用例：查询死信、人工重放。重放是否安全的判定（哪些失败可以直接重放，哪些必须人确认）落在这里，而不是 transport。

## 职责与边界

- 负责：任务列表的状态过滤校验、重放前的安全判定（`domain.ReplaySafe`）、`force` 语义、把“失败任务放回 pending”交给存储。
- 不负责：任务的入队与执行（`worker`）、重试退避（`worker`）、HTTP 编解码与授权（`transport/admin`）、SQL（`store`）。
- 此前这条规则写在 `transport/admin/jobs.go` 的 `retryJob` 里。上游 `/product/add` 没有幂等键，重放判错会写出第二条记忆，所以它是业务规则，不是协议细节。

## 依赖

- 依赖 `internal/domain`。
- 以使用方定义的窄接口依赖存储，不 import adapter：

```go
type Store interface {
    ListJobs(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error)
    GetJob(ctx context.Context, id string) (domain.Job, error)
    // RequeueJob 在事务内判状态并放回 pending，只接受 failed。
    RequeueJob(ctx context.Context, id string, now time.Time) (domain.Job, error)
}
```

## 契约

```go
func New(store Store) *Service

func (s *Service) List(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error)
func (s *Service) Retry(ctx context.Context, id string, force bool) (domain.Job, error)
```

- List：`Status` 非空时必须是 `pending` / `running` / `succeeded` / `failed`，否则 `ErrInvalidInput`；其余条件原样交给存储，存储负责分页上限。不返回 payload（存储层已排除）。
- Retry：
  - `id` 为空 → `ErrInvalidInput`。
  - 未带 `force`：先读任务，`last_error` 不是 `domain.ReplaySafe` 认定的类型（空串与未知类型一律按不安全）→ `ErrConflict`，消息里带上失败类型，提示“确认上游没写成功再加 force=true”。
  - 带 `force` 或判定安全：交给 `RequeueJob`。非 failed 的任务由存储返回 `ErrConflict`，不存在返回 `ErrNotFound`；并发重放时后到者得到 `ErrConflict`。
  - 带 `force` 时不读任务，直接交给存储，因此不存在的 id 同样得到 `ErrNotFound`。

## 错误与边界情况

- 重放后 attempts 归零、`last_error` 清空、立即可被 worker 认领；该服务不提供“强制把运行中任务重置”。
- 判定只看 `last_error` 的类型名：控制面不解析 payload，也不查上游是否真的写入。
- 读后写不是同一事务：判定与 `RequeueJob` 之间任务被别的重放改过，由存储的条件 UPDATE 兜底。

## 测试与验证

- `internal/service/jobs` 用假存储覆盖：状态过滤、`id` 为空、安全/不安全错误类型、`force` 绕过、非 failed、不存在、并发冲突透传、`now` 的传递。
- `transport/admin` 的 HTTP 用例（列表、重放 200/409/404、租户 Token 被拒）继续作为行为回归。

## 参考来源

- 迁移前的实现：`internal/transport/admin/jobs.go`（见提交历史）。
- 重放安全口径：`internal/domain/errors.go` 的 `jobReplaySafe`；任务模型见 [worker](worker.md)。

## 实现记录

See the public version-control history.
