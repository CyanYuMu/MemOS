# API 契约 v2

这份文档描述**调用方可以依赖的东西**。它对应 `GET /product/contract` 返回的机器可读版本
（`contract_version`）；控制面与 MCP 网关在启动时比对该字段，不认识的版本不放流量。

改动这里之前先问一句：调用方会不会因此静默算错？会，就升 `CONTRACT_VERSION`
（`src/memos/api/contract.py`），并在 `CHANGELOG.md` 写明。

## 1. 时间：一律是真实 UTC

**所有响应里的时间字段都是 RFC 3339 的真实 UTC。调用方不做任何偏移换算。**

涉及字段（`memos.time_basis.TIME_FIELDS`）：`created_at`、`updated_at`、`chat_time`、
`valid_from`、`valid_to`、`event_time_start`、`event_time_end`、`expires_at`、
`last_dream_update`、`archived_at`；图/事件投影里的 `createdAt`、`updatedAt`。

### 为什么需要一层转换

部署里存在两代数据：

| 写入时间 | 值 | 语义 |
| --- | --- | --- |
| T1.3 修复前（普通写入） | 北京墙钟 + 后缀 `+00:00` | **假 UTC**，实际比真 UTC 晚 8 小时 |
| T1.3 修复前（Dream 产物） | UTC | 真 UTC |
| 现在（全部写入） | `utc_now_iso()` | 真 UTC |

两类历史数据只看值分不开，所以节点上带 `metadata.time_basis`：

- `"utc"`：真 UTC（所有新写入都打这个标记）。
- 缺失 / `"legacy_local"`：按 `MEMOS_LEGACY_TIME_OFFSET`（默认 `+08:00`）重新解释。
- Dream 三类（`DreamDiary`、`InsightMemory`、`Context`）无论有无标记都按真 UTC 处理。

API 边界（`_shape_response`）统一转换后再返回，嵌套结构（`history`、`timespec`）继承所属节点的 basis。
`order_pair` / `version_time` 这类内部比较也按各自节点的 basis 归一后再比。

### 写入侧

- 所有写入路径用 `utc_now_iso()`。
- 图库写入口（`_prepare_node_metadata`）打 `time_basis="utc"`；`import_graph` 导入外部导出数据时
  保留原 basis（`stamp_basis=False`）。
- 合并路径先按各自 basis 把两边转成真 UTC 再比较 `created_at` / `chat_time`。
- 版本链（`organize/versioning.py`）在把旧版本快照进 `history` 之前同样按节点 basis 归一，
  否则一个刚被标成 `utc` 的节点里会混进北京墙钟的历史条目。
- **懒迁移**：`update_node` 遇到没有标记的旧节点时，会在同一次写入里把该节点其余时间字段一并
  转成真 UTC 并打标记，避免一个节点同时存在两种口径。

## 2. `metadata.sources` 默认不返回

单条记忆的 `sources` 可能到几百 MB（历史上因重复 `json.dumps` 指数膨胀，实测单条 132 MB），
而没有任何控制台消费者读它。

- 读接口（`/product/search`、`/product/get_memory`、`/product/get_memory/{id}`、
  `/product/get_memory_by_ids`、`/product/get_memory_dashboard`、`/product/get_all`、
  `/product/get_memory_graph`、`/product/get_event_memories`）默认删掉 `metadata.sources`。
- 需要时显式传 `include_sources: true`（`GET /product/get_memory/{id}` 用查询参数）。
- 历史膨胀数据不会被读侧裁剪缩小，仍需一次性重写脚本处理存储层。

## 3. `get_memory_by_ids`：对象体

```json
POST /product/get_memory_by_ids?mem_cube_id=<cube>
{"memory_ids": ["m1", "m2"], "include_sources": false}
```

- 主形状是对象；裸数组 `["m1", "m2"]` 仍然接受，供未迁移的调用方使用，**不要**用它写新代码。
- `mem_cube_id` 是查询参数：给了就只返回该租户的记忆；不给就落到服务配置的默认租户
  （跨租户的 id 会静默变成空数组，这是最容易踩的坑）。
- 单条 `GET /product/get_memory/{id}` 不带 `mem_cube_id` 时跨租户查找，用于归属反查。

## 4. 错误：业务失败是 HTTP 4xx

参数组合错误（`delete_memory` 同时给多个删除模式、一个都没给等）返回 HTTP 400，
不再是 `200` + `data.status=failure`。调用方可以直接以状态码判成功与否。

## 5. 其它约束

- `top_k` 上限 25（`contract.TOP_K_MAX`，对齐云端召回上限）。
- Dream 类记忆的类型名与「真 UTC」的对应关系见第 1 节，新增 Dream 类型时必须同步
  `memos.time_basis.DREAM_MEMORY_TYPES`。

## 6. 升级检查清单

1. 改 `src/memos/api/` 后跑 `make openapi`，把 `docs/openapi.json` 一起提交。
2. 有破坏性变更时升 `CONTRACT_VERSION`，更新本文与 `CHANGELOG.md`。
3. 跑 `pytest tests/api`（`tests/api/test_time_basis.py`、`tests/api/test_contract_api.py`
   锁住时间口径与响应形状）。
