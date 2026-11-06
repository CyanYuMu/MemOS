# 幻觉守卫（hallucination_guard）

## 解决的问题

记忆由 LLM 从对话中抽取。抽取模型偶尔会写出原文没有的内容，或与原文矛盾的结论。这类记忆入库后，会在后续会话被检索出来当作事实使用，比"没记住"更糟。

例如：助手猜测用户喜欢喝茶，但用户从未表达过这一偏好。如果这句话被当作 PreferenceMemory 存入，后续检索可能把猜测当作事实。

幻觉守卫在写库前加一道校验：把每条待写入的记忆与原始对话比对，保留有原文支撑的条目，其余丢弃或记日志。校验 prompt 要求"记忆必须是用户明确说过的话的直接反映"，因此既能拦下与原文矛盾的条目，也能拦下无据编造。

当前状态：影子模式，只记日志，不丢弃任何条目。

## 为什么之前没有这道校验

上游有现成的过滤函数 `filter_hallucination_in_memories`，但只接在 `SimpleStructMemReader._read_memory`，且要 `SIMPLE_STRUCT_ADD_FILTER=true` 才生效。本部署的 reader 是多模态版（`MEM_READER_BACKEND` 默认 `multimodal_struct`），覆写了 `_read_memory`，那段接线走不到。也就是说，本部署此前没有任何事后校验。

守卫以插件形式补上这段接线。过滤函数与 prompt 复用上游，未自造。

## 覆盖范围

| 路径 | 触发点 | 产出 |
|---|---|---|
| 对话抽取 | `multi_modal_struct._process_string_fine`，逐窗口 | LongTermMemory / UserMemory |
| 偏好抽取 | `multi_modal_struct._process_multi_modal_data`，`process_preference_fine` 返回后 | PreferenceMemory |

两条路径都在写库前触发。Dream 生成的 Diary / Insight 不经过这里。

## 配置

插件由 `MEMOS_ENABLED_PLUGINS` 控制是否加载；加载后由三个变量控制行为：

| 变量 | 默认 | 作用 |
|---|---|---|
| `MEMOS_HALLUCINATION_GUARD` | `false` | 总开关。false 时回调直接返回，不调用 LLM |
| `MEMOS_HALLUCINATION_GUARD_PREF` | `true` | 是否过滤偏好路径。false 时只过滤对话路径 |
| `MEMOS_HALLUCINATION_GUARD_ENFORCE` | `false` | false 为影子模式，只记日志；true 才丢弃被标记的条目 |

常用组合：

```
# 影子运行：只记日志，写入行为不变
MEMOS_ENABLED_PLUGINS=dream,hallucination_guard
MEMOS_HALLUCINATION_GUARD=true
MEMOS_HALLUCINATION_GUARD_ENFORCE=false

# 正式生效
MEMOS_HALLUCINATION_GUARD_ENFORCE=true

# 停用：保留插件加载，不产生 LLM 调用
MEMOS_HALLUCINATION_GUARD=false
```

## 日志

| 日志 | 含义 |
|---|---|
| `[hallucination_guard][chat] all N item(s) kept` | 对话路径判定完成，全部保留 |
| `[hallucination_guard][pref] all N item(s) kept` | 偏好路径同上 |
| `[hallucination_guard][chat] WOULD-DROP \| <文本>` | 影子模式：该条被标记，未丢弃 |
| `[hallucination_guard][chat] DROPPED \| <文本>` | 强制模式：该条已丢弃 |
| `[hallucination_guard][chat] filter failed; keep items unchanged` | 过滤异常，原样放行 |

过滤异常、LLM 限流、判定解析失败时一律原样返回，不阻塞写入。

## 风险

- 过滤 prompt 判定严格，要求记忆是"用户明确说过"的。`implicit_preference` 属行为归纳，可能被误判。切强制模式前需人工核对影子数据。
- 每个窗口/chunk 增加一次 general_llm 调用。oneapi 限流紧张时守卫 fail-open，影子数据会变稀疏。
- 只覆盖抽取环节；Dream 产物与其他写入路径不在范围内。

## 相关文件

- 插件代码：`src/memos/hallucination_guard/`
- 偏好路径触发点：`src/memos/plugins/hook_defs.py`、`src/memos/mem_reader/multi_modal_struct.py`
