---
module: platform
packages:
  - internal/platform/num
  - internal/platform/page
status: implemented
---

# platform

放与业务无关、又被多个包共用的小工具。目前只有整数边界处理与内存分页。

## 职责与边界

- `num.Clamp` 限制区间，`num.WithDefault` 在零值时取默认再限制区间。
- `page.Slice` 对已取出的切片做内存分页，页码从 1 开始，越界返回空切片。
- 不负责解析 HTTP 入参、不负责数据库分页、不记录日志：调用方决定默认值与上限。

## 依赖

- 只依赖标准库，属于叶子包；service、transport、adapter 都可以引用。
- 被 `internal/service/memory`、`internal/service/account`、`internal/transport/dashboard`、
  `internal/adapter/upstream` 使用。

## 契约

```go
func num.Clamp(v, lo, hi int) int
func num.WithDefault(v, def, lo, hi int) int
func page.Slice[T any](items []T, p, size int) []T
```

- `WithDefault` 只在 `v == 0` 时取 `def`；负数会被 `Clamp` 压到 `lo`。
- `Slice` 的 `p < 1`、`start` 越界时返回空切片而不是 `nil`，便于 JSON 序列化成 `[]`。
- 不做数据库 LIMIT/OFFSET：数据量在内存里已经过滤完毕时才用。

## 错误与边界情况

- 纯函数，不返回错误；非法区间由调用方保证 `lo <= hi`。
- `size <= 0` 时 `Slice` 返回空切片，不 panic。

## 测试与验证

- 覆盖区间边界、零值取默认、页码越界；随使用方的单元测试一起跑。

## 参考来源

- 原实现分散在 `internal/transport/dashboard`、`internal/service/memory`、`internal/service/account`、
  图谱适配器（2026-10-23 已删除）的 `clamp`/`paginate`，2026-10-22 合并到这里。

## 实现记录

See the public version-control history.
