# 记忆提示词注册表

这份文档是 dumemos 里所有送给大模型的记忆相关提示词的唯一盘点结果，也是 `src/memos/mem_reader/prompt_registry.py` 的数据来源。控制面和前端不保存默认文本，全部通过 `/product/prompts/*` 从这里取。

状态：注册表（19 条，含样例输入）、版本存储、接口、抽取与 Dream 的运行时生效均已完成。

## 使用说明

本机只跑提示词接口（供控制面联调，监听 18101）：

```bash
VENV=.venv/bin
export PYTHONPATH=$PWD/src
set -a; source <任务目录>/env.local; set +a          # 可选，没有 LLM 时注册表接口照常可用
MEMOS_PROMPT_CONFIG_DB=/tmp/prompts.db $VENV/python scripts/dev/prompt_service.py --port 18101
```

常用调用（全部 `POST`，前缀 `/product/prompts`）：

```bash
curl -s localhost:18101/product/prompts/list    -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a"}'
curl -s localhost:18101/product/prompts/detail  -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a","key":"extract.chat"}'
curl -s localhost:18101/product/prompts/versions -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a","key":"extract.chat","lang":"zh"}'
curl -s localhost:18101/product/prompts/validate -H 'content-type: application/json' -d '{"key":"extract.chat","lang":"zh","body":"规则\n${conversation}"}'
curl -s localhost:18101/product/prompts/save    -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a","key":"extract.chat","lang":"zh","body":"规则\n${conversation}","base_version_id":0,"author":"alice"}'
curl -s localhost:18101/product/prompts/test    -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a","key":"extract.chat","lang":"zh","input":{"messages":[{"role":"user","content":"..."}]},"compare_default":true}'
curl -s localhost:18101/product/prompts/generate -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a","key":"extract.chat","lang":"zh","instruction":"只抽取部署相关的事实"}'
curl -s localhost:18101/product/prompts/reset   -H 'content-type: application/json' -d '{"mem_cube_id":"cube-a","key":"extract.chat","lang":"zh"}'
```

相关环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `MEMOS_PROMPT_CONFIG_DB` | `<MOS_CUBE_PATH 或 MEMOS_DIR>/extract_prompts.db` | 提示词版本库（SQLite，落在 PVC 上） |
| `PROMPT_TEST_MAX_CONCURRENCY` | `4` | 试跑全局并发上限 |
| `PROMPT_SERVICE_PORT` | `18101` | 开发服务端口 |
| `MEM_READER_BACKEND` | `multimodal_struct` | 抽取试跑用哪个 reader（`simple_struct` / `multimodal_struct`） |

旧接口（`get_extract_prompt` / `save_extract_prompt` / `reset_extract_prompt` / `generate_extract_prompt` / `debug_extract` / `models`）仍挂在同一前缀下，内部映射到 `extract.chat` 槽位，响应形状不变。

## 注册项字段

每条注册项对应一种提示词，可以被某个项目、某种语言单独覆盖。字段与 `spec.md` 第 3 节一致：

- `key`：`<group>.<name>`，全局唯一，控制面前端都用它做标识。
- `group`：`extract`（抽取）、`process`（抽取后的处理）、`dream`（自动整理）。
- `title` / `summary`：中英双语，给界面用。
- `languages`：这条提示词实际存在默认文本的语言。项目覆盖只对这些语言开放。
- `syntax`：模板占位符的替换方式，决定校验方式与前端高亮规则，见下一节。
- `placeholders`：占位符名字、是否必填、中英文说明。
- `locked_suffix`：代码在用户正文之后自动追加的固定文本（例如 Dream 动机的严格输出后缀），用户不可编辑。
- `input_kind` / `input_schema`：试跑输入的类型与 JSON Schema。
- `output_contract`：模型应该返回的结构，试跑用它判断输出是否符合契约。
- `sample_inputs`：1 到 3 条真实样例，中英文各至少一条。
- `editable`：是否允许项目覆盖。

## 语法（syntax）

代码里实际有三类替换方式，注册表的 `syntax` 必须如实记录，否则校验会把合法的正文判成非法：

- `dollar`：`text.replace("${name}", value)`。对话抽取用它，`${conversation}` 与 `${custom_tags_prompt}` 写在正文里，正文其余花括号是普通字符。
- `format`：`text.format(**values)`。事件抽取与 Dream 四条用它，正文里的字面花括号必须写成双花括号。
- `brace`：`text.replace("{name}", value)`。文档与通用文本、偏好、工具轨迹用它。它含大量字面 JSON 花括号，不能过 `str.format`，但占位符仍是 `{name}` 形式。

`brace` 的校验只检查每个必填占位符以 `{name}` 的形式逐字出现，正文里其余花括号（JSON 示例等）原样保留、不做 `str.format` 检查。事件与 Dream 属于 `format`，文档、通用文本、偏好、工具轨迹属于 `brace`。

## 盘点结果

行号是 2026-10-29 在 `feat/project-prompts` 基线（master@b486fdd8）上的位置。

| key | 默认常量 | 位置 | 语言 | 占位符 | 语法 | 输出 | 调用方 | 使用的模型 |
|---|---|---|---|---|---|---|---|---|
| `extract.chat` | `SIMPLE_STRUCT_MEM_READER_PROMPT` / `_ZH` | `templates/mem_reader_prompts.py:1` / `:113` | zh, en | `${conversation}`（必填）、`${custom_tags_prompt}` | dollar | `{"memory list":[{"key","memory_type","value","tags"}], "summary"}` | `simple_struct.py:_get_llm_response`、`multi_modal_struct.py:_get_llm_response` | reader 主模型（`MEMRADER_MODEL`） |
| `extract.doc` | `SIMPLE_STRUCT_DOC_READER_PROMPT` / `_ZH` | `:251` / `:310` | zh, en | `{chunk_text}`（必填）、`{custom_tags_prompt}` | brace | 同上 | `multi_modal_struct.py:_get_llm_response(prompt_type="doc")`、`simple_struct.py:_process_doc_data` | reader 主模型 |
| `extract.general_string` | `GENERAL_STRUCT_STRING_READER_PROMPT` / `_ZH` | `:406` / `:514` | zh, en | `{chunk_text}`（必填）、`{custom_tags_prompt}` | brace | 同上 | `multi_modal_struct.py:_get_llm_response(prompt_type="general_string")` | reader 主模型 |
| `extract.preference_explicit` | `NAIVE_EXPLICIT_PREFERENCE_EXTRACT_PROMPT` / `_ZH` | `templates/prefer_complete_prompt.py:1` / `:42` | zh, en | `{qa_pair}`（必填） | brace | JSON 数组，元素含 `explicit_preference`、`context_summary`、`reasoning`、`topic` | `read_pref_memory/process_preference_memory.py:_extract_explicit_preference` | `preference_extractor_llm`（缺省回落 `general_llm`） |
| `extract.preference_implicit` | `NAIVE_IMPLICIT_PREFERENCE_EXTRACT_PROMPT` / `_ZH` | `:83` / `:148` | zh, en | `{qa_pair}`（必填） | brace | JSON 数组，元素含 `implicit_preference`、`context_summary`、`reasoning`、`topic` | 同上 `_extract_implicit_preference` | 同上 |
| `extract.event` | `EVENT_EXTRACTION_PROMPT` | `mem_reader/event_extractor.py:34` | en | `{conversation}`（必填） | format | JSON 数组，元素含 `title`、`content`、`event_type`、`time` 等 | `multi_mem_cube/single_cube.py:922` → `EventExtractor.extract` | `general_llm` |
| `extract.tool_trajectory` | `TOOL_TRAJECTORY_PROMPT_EN` / `_ZH` | `templates/tool_mem_prompts.py:69` / `:1` | zh, en | `{messages}`（必填） | brace | JSON 对象，含 `correctness`、`trajectory`、`experience`、`tool_used_status` | `multi_modal_struct.py:863` | `general_llm` |
| `process.hallucination_filter` | `SIMPLE_STRUCT_HALLUCINATION_FILTER_PROMPT` | `mem_reader_prompts.py:854` | en | `{messages_inline}`、`{memories_inline}` | format | JSON 对象，索引到 `{"keep": bool, "reason": str}` | `simple_struct.py:530` `filter_hallucination_in_memories`，也被 `hallucination_guard` 插件复用 | `general_llm` |
| `dream.motive` | `MOTIVE_FORMATION_PROMPT` | `dream/prompts/motive_prompt.py:1` | en | `{memories_block}`、`{max_motives}` | format | JSON 数组，元素含 `motive_id`、`description`、`memory_ids` | `dream/pipeline/motive.py:47` | 产品主模型（`shared.llm`） |
| `dream.reasoning` | `CONSOLIDATION_REASONING_PROMPT` | `dream/prompts/reasoning_prompt.py:1` | en | `{motive_description}`、`{source_memories_block}`、`{related_memories_block}` | format | JSON 对象，含 `dream_content`、`hypothetical_question` | `dream/pipeline/reasoning.py:80` | 产品主模型 |
| `dream.context_binding` | `CONTEXT_BINDING_PROMPT` | `dream/prompts/context_binding_prompt.py:1` | en | `{memories_block}` | format | JSON 对象，含 `contexts[]`，每项 `key`、`ids`、`confidence`、`reason` | `dream/contextualization.py:303` | 产品主模型 |
| `dream.context_summary` | `CONTEXT_SUMMARY_PROMPT` | `dream/prompts/context_summary_prompt.py:1` | en | `{memories_block}`、`{existing_key}`、`{existing_memory}` | format | JSON 对象，含 `key`、`memory`、`confidence` | `dream/contextualization.py:437` | 产品主模型 |

`dream.motive` 的 `locked_suffix` 是 `dream/pipeline/motive.py:52-57` 追加的那段 `OUTPUT FORMAT (STRICT)` 文本，只读展示，不属于用户正文。

### 与 spec 列举范围的差异

- 偏好抽取拆成 `extract.preference_explicit` 与 `extract.preference_implicit` 两条：两个提示词输出字段不同（`explicit_preference` / `implicit_preference`），合成一条会让试跑和契约校验说不清。spec 里举的例子是 `extract.preference`，已写进 question 文件。
- 通用文本抽取单列为 `extract.general_string`：它和文档抽取是两个模板、两条 `prompt_type` 分支，合并不合适。

## 刻意排除的项

| 内容 | 位置 | 排除理由 |
|---|---|---|
| 图片解析 `IMAGE_ANALYSIS_PROMPT_EN/_ZH` | `mem_reader_prompts.py:671` / `:728` | spec 第 2 节明确本期不做，试跑需要图片输入 |
| 记忆合并 `MEMORY_MERGE_PROMPT_EN` / `_ZH` | `:925` / `:1006` | 代码里没有调用方，只登记在 `PROMPT_MAPPING` 里。没有调用方的提示词不进注册表，也不在控制台展示：改了没有效果的入口比没有入口更糟。今后接入的条件是合并步骤真的被某条写入路径调用，并且调用处改成经过 `prompt_for("process.merge", ...)` |
| 记忆改写 `SIMPLE_STRUCT_REWRITE_MEMORY_PROMPT`、`_USER_ONLY`、`SIMPLE_STRUCT_ADD_BEFORE_SEARCH_PROMPT` | `:785` / `:818` / `:899` | 同上：代码里没有调用方，只登记在 `PROMPT_MAPPING` 里；接入条件同上 |
| `CUSTOM_TAGS_INSTRUCTION(_ZH)` | `:661` / `:666` | 不是独立提示词，是 `${custom_tags_prompt}` / `{custom_tags_prompt}` 占位符的内容，由代码在替换阶段填入 |
| 提示词里的示例段 `SIMPLE_STRUCT_MEM_READER_EXAMPLE(_ZH)` | `:573` / `:617` | 属于 `extract.chat` 默认正文的一部分（默认模板自带），`remove_prompt_example` 时才被剥掉 |
| 偏好判重与合并 `NAIVE_JUDGE_*`、`PREF_INSTRUCTIONS(_ZH)` | `prefer_complete_prompt.py:213` 起 | 是偏好记忆的写入判定（去重、更新、操作轨迹），属于算法内部判据，不在 spec 第 3 节的范围里 |
| 检索、调度、反馈、agent、时间意图等提示词 | `templates/*_prompts.py` 其余文件 | 与"把对话/文档整理成记忆"无关，不在本任务范围 |

## 默认值不可变

注册表里的内置默认必须与重构前逐字节相同。`tests/mem_reader/test_prompt_store.py::test_every_default_is_byte_identical_to_the_pre_registry_text` 对每条默认断言 sha256，重构期间任何文字改动都会让它失败。

## 语言

- `extract.chat`、`extract.doc`、`extract.general_string`、偏好、工具轨迹有中英两份；`extract.event`、`process.hallucination_filter` 只有英文；Dream 四条只有英文。
- 某语言的槽位没有项目覆盖时用该语言的内置默认，绝不拿另一种语言的覆盖顶替。

## 运行时如何生效

`prompt_runtime.prompt_scope(mem_cube_id)` 在每个会调用 LLM 的入口包一次，把该项目的覆盖一次性读进 ContextVar；`prompt_for(key, lang, default)` 在抽取/处理代码里取模板，不在 scope 内或没有覆盖就返回传入的内置默认。

已接入的入口：

- 同步 add：`src/memos/api/handlers/add_handler.py` 的 `handle_add_memories`。
- 试跑抽取：同一文件的 `handle_extract_memories`（`/product/extract_memory`）。
- 异步 add 的读取：`src/memos/mem_scheduler/task_schedule_modules/handlers/mem_read_handler.py` 的 `_process_memories_with_reader`。这里用 `mem_cube_id` 而不是 `user_name` 开 scope：两者可能不一致，按 `user_name` 开会读不到项目覆盖。`tests/mem_reader/test_prompt_override_paths.py::test_async_read_path_scopes_prompts_to_the_cube_not_the_user_name` 固定这一点。
- Dream：`src/memos/mem_scheduler/task_schedule_modules/handlers/mem_dream_handler.py` 的 `_run_dream` 用 `mem_cube_id`（缺省回落 `user_name`）开 scope，包住整个 pipeline 钩子调用。

Dream 四步的取用点：`dream/pipeline/motive.py`、`dream/pipeline/reasoning.py`、`dream/contextualization.py`（`dream.context_binding` 与 `dream.context_summary`）。`dream.motive` 的 `locked_suffix` 定义在 `dream/prompts/motive_prompt.py` 的 `MOTIVE_OUTPUT_SUFFIX`，代码与人看的说明用的是同一份常量。

已接入的提示词取用点（全部经过 `prompt_for`）：

- `simple_struct._get_llm_response`（`extract.chat`）、`simple_struct._process_doc_data`（`extract.doc`）、`simple_struct.filter_hallucination_in_memories`（`process.hallucination_filter`）。
- `multi_modal_struct._get_llm_response`（`extract.chat` / `extract.doc` / `extract.general_string`）、`_get_llm_tool_trajectory_response`（`extract.tool_trajectory`）。
- `event_extractor.EventExtractor._extract_window`（`extract.event`）。
- `read_pref_memory.process_preference_memory`（`extract.preference_explicit` / `extract.preference_implicit`）。

语言：对话、文档、通用文本、偏好、工具轨迹按现有的 `detect_lang` 选槽位；只有英文的 `extract.event`、`process.hallucination_filter` 与 Dream 四条固定传 `en`，否则中文输入永远取不到它们的覆盖。项目覆盖渲染失败（花括号、缺占位符）时记一条 warning，这一次调用退回内置默认，异常不会冒到抽取流水线外。

## Dream 的运行器

`src/memos/dream/dry_run.py` 的 `run_dream_stage(key, memories, llm=...)` 直接调用真实阶段类（`MotiveFormation`、`ConsolidationReasoning`、`DreamContextualizer`），只把 `graph_db` 换成内存节点表、把 LLM 包一层记录原始输出。返回 `output`、`raw_output`、`parse_ok`、`fallback_triggered`（动机/推理/上下文摘要的兜底路径会在输出里留下 `Fallback:` 或 `strategy != "llm"` 的痕迹）。

## 试跑与改写

`/product/prompts/test` 的实现在 `src/memos/mem_reader/trial.py`：草稿通过 `draft_prompt_override` 注入 ContextVar，不落库；跑完把 ContextVar 还原。

每种 key 走哪段真实代码：

| key | 走的代码 |
|---|---|
| `extract.chat` | `reader.get_memory(..., mode="fine")`，reader 由 `MEM_READER_BACKEND` 决定（默认 multimodal_struct） |
| `extract.doc` / `extract.general_string` | `MultiModalStructMemReader._get_llm_response(prompt_type=...)`；只有这个 reader 有 general_string 分支，所以固定用它 |
| `extract.tool_trajectory` | `_get_llm_tool_trajectory_response` |
| `extract.event` | `EventExtractor.extract` |
| `extract.preference_explicit` / `_implicit` | `_extract_explicit_preference` / `_extract_implicit_preference` |
| `process.hallucination_filter` | `filter_hallucination_in_memories` |
| `dream.*` | `src/memos/dream/dry_run.py` 的真实阶段类 |

`parse_ok` 用 `parse_json_result` 判断模型原始输出；`parse_json_result` 对非 JSON 返回 `{}`，所以空结果也算失败。`fallback_triggered` 在抽取侧等于 `not parse_ok`（reader 会把原文整段存成一条 UserMemory），Dream 侧取阶段自己的兜底痕迹。

限额：全局并发 `PROMPT_TEST_MAX_CONCURRENCY`（默认 4），同一 `mem_cube_id` 同时在途 1 个，超出返回 429 `busy` 与 `retry_after_sec`。单次调用超时 90 秒，超时那次 run 记 `llm_timeout`，整个请求仍是 200。输入上限：对话 30 条消息、总计 12000 字符；正文 20000 字符（校验阶段）；`validate_input` 在调用模型之前完成，超限返回 413 `input_too_large`。

`/product/prompts/generate` 用 registry 里的标题、占位符与语法生成改写指令，结果必须通过静态校验；不通过时自动重试一次，仍不通过就原样返回并带上 `validation`，由前端提示。模型失败映射 502 `llm_error`，超时映射 504 `llm_timeout`。

## 试跑用的 Reader

`src/memos/mem_reader/dry_run.py` 的 `build_dry_run_reader(backend)` 用与数据面相同的 LLM、chunker 配置构造真实 reader，只把嵌入器换成 `placeholder`（`src/memos/embedders/placeholder.py`，按文本哈希生成稳定向量，没有语义）。试跑与 S4 的实跑测试都用它，不需要图库、向量库或 Redis。

## 部署与控制面

数据面的 `/product/prompts/*` 本身不做鉴权：`server_api` 不挂认证中间件，这些接口只假定被控制面这类内部调用方访问。不要把它直接暴露到公网；需要外部访问时由控制面代转发并在那一层做登录与鉴权。

试跑的全局信号量（`PROMPT_TEST_MAX_CONCURRENCY`）与"同一 `mem_cube_id` 同时在途 1 个"的计数都在进程内存里（`TrialLimiter`），只有单副本部署时才是全局限额；多副本时每个副本各自计数，实际并发是副本数乘以限额。换成多副本前要先把计数挪到共享存储，或者按副本数调小限额。

一次 `extract.chat` 试跑通常只有 1 次模型调用；输入触发偏好、skill 等分支时会有更多次，实际次数记在该 run 的 `usage.calls` 里。`compare_default: true` 要多跑一遍内置默认，模型调用翻倍（通常是 2 次）。控制面因此按每次试跑 2 个令牌计。

## 运行时不依赖图库与向量库

提示词的读写、校验、试跑只依赖 SQLite 和 LLM，实测见 `inbox/infra.status.md` 的 P0 记录：

- 抽取试跑走 `SimpleStructMemReader.get_memory` / `MultiModalStructMemReader` 的真实路径，唯一需要占位的是嵌入器（记忆条目构造时会算向量）。试跑用一个确定性的占位嵌入器，向量不落库也不参与比较。
- Dream 的动机与推理两步只需要一个能按 id 取回记忆正文的对象（真实部署里是 `graph_db`）。试跑用内存里的节点表提供同样的接口，不连图库。
