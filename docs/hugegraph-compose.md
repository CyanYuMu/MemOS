# HugeGraph 原型 Compose 启动

`docker/docker-compose.hugegraph.yml` 会启动：

- MemOS Python API
- Qdrant 向量数据库

HugeGraph 不包含在 Compose 中，需要提前准备好 HugeGraph Server、GraphSpace 和 graph，并通过根目录 `.env` 中的 `HUGEGRAPH_*` 变量连接。

## 启动

```bash
cp docker/.env.hugegraph.example .env
# 编辑 .env，填写 LLM、embedding、HugeGraph 凭据
cd docker
docker compose -f docker-compose.hugegraph.yml up --build
```

验证：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:6333/healthz
```

首次启动前，HugeGraph 中必须已经存在对应 graph，例如 `memory/mem_graph`，并执行
`docker/hugegraph-schema.groovy` 创建 MemOS provider 所需 schema。当前 provider 不会在运行时
修改 schema，`HUGEGRAPH_AUTO_CREATE_SCHEMA` 不是受支持的配置项。

`Memory` 顶点标签必须使用 `CUSTOMIZE_STRING` ID 策略。这样 HugeGraph 顶点 ID、MemOS memory
ID 与向量库 point ID 才是同一个 UUID。旧原型如果已用 `PRIMARY_KEY` 创建了 `Memory`，
`ifNotExist()` 不会改变已有标签；请先导出旧数据，在一个新 graph 上执行最新 schema 后再导入。
不要直接在原 graph 上覆盖 ID 策略。

当前 `hugegraph-python` 客户端固定使用 HTTP。`HUGEGRAPH_URL` 因此只接受 `http://`；需要
TLS 时，应在受信网络内通过反向代理终止 TLS，并让 MemOS 到 HugeGraph 的内部链路使用 HTTP。

## 网络地址规则

MemOS 容器内连接 Qdrant 使用 `qdrant:6333`，Compose 已自动注入。连接宿主机上的 HugeGraph 时，macOS/Windows 可使用 `host.docker.internal`；Linux 请将 `HUGEGRAPH_URL` 改成宿主机可达 IP，或使用实际的 HugeGraph 服务地址。

HugeGraph 的向量索引通过 `HUGEGRAPH_VECTOR_BACKEND` 选择，支持 `qdrant`（默认）、`milvus` 和
`none`。选择 Milvus 时设置 `MILVUS_URI`、`MILVUS_USER_NAME`、`MILVUS_PASSWORD`，并可用
`HUGEGRAPH_MILVUS_COLLECTION_NAME` 指定集合；选择 `none` 可只使用图存储，不初始化外部向量库。

默认 embedding 配置使用外部 OpenAI-compatible 服务。Qdrant 只负责存储和相似度检索，不负责生成 embedding。

## 已知缺口（TODO）

HugeGraph 后端的接入已经打通链路，但仍有若干未完成点，代码中统一以 `TODO(hugegraph)`
标记，可 `grep -rn "TODO(hugegraph)" src docker docs` 一次看全。清单如下（行号为当前分支）：

| 位置 | 缺口 |
|---|---|
| `src/memos/graph_dbs/hugegraph.py:661` `get_subgraph` | 签名缺 `center_status`，而 `memories/textual/tree.py:319` 会传该参数（Neo4j 后端支持）→ 线上召回路径直接 `TypeError` |
| `hugegraph.py:742` `search_by_embedding` | `filter` / `search_filter` / `knowledgebase_ids` 被 `**kwargs` 吞掉，且未走 `_validate_return_fields` → 跨 cube 泄漏、返回字段不受控 |
| `hugegraph.py:802` `search_by_fulltext` | 只取 `query_words[0]`；`filter` / `search_filter` / `threshold` / `tsquery_config` / `knowledgebase_ids` 被吞 |
| `hugegraph.py:835` `get_by_metadata` | 只把 `memory_type` / `status` / `user_name` 的 `=` 条件下推；其余算子与 `metadata_json` 字段在取回全部租户顶点后客户端过滤 |
| `hugegraph.py:943` `get_all_memory_items` | `filter` / `knowledgebase_ids` 被吞 |
| `hugegraph.py:1107` `export_graph` | `page` / `page_size` / `filter` / `status` 被丢弃 → 分页导出会返回整个租户 |
| `hugegraph.py:959` `get_structure_optimization_candidates` | 基类声明的 `include_embedding` 被吞；status / cube 范围硬编码为 `activated` |
| `hugegraph.py:572` `search_by_kneighbor` | 只填 `neighbors`，`paths` / `vertices` / `edges` 恒为空；当前无调用方 |
| `hugegraph.py:1147` `deduplicate_nodes` / `detect_conflicts` / `merge_nodes` | 属 `BaseGraphDB` 契约但抛 `NotImplementedError`；当前无生产调用方，故暂为惰性桩 |
| `hugegraph.py:91` 类定义 | 无 schema / 索引管理（Neo4j 侧有 `create_index` / `_ensure_database_exists`），schema 只能手工执行 `docker/hugegraph-schema.groovy`，启动时不校验 |
| `hugegraph.py:134` `__init__` | 未解析到向量索引时 `_vec_*` 变空操作、`search_by_embedding` 返回 `[]` → 语义召回无声失败 |
| `docker/hugegraph-schema.groovy` | 无 `delete_record_id` / `delete_time` 索引；索引是否命中不在启动时校验 |
| `docker/docker-compose.hugegraph.yml` | 仅开发 / 演示用：不部署 HugeGraph 本体，不加载 schema |


要把 memmy-agent 自带 Desktop/Agent Runtime 接到该存储链路，参见
[memmy-agent Desktop 接入 MemOS HugeGraph](memmy-agent-hugegraph.md)。需要按步骤部署和验收时，
参见[带逐步流程图的本地部署指南](memmy-agent-memos-deployment.md)。
