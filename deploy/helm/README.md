# MemOS Helm Chart

把 MemOS 部署到 Kubernetes。`values.yaml` 里的每一项都可以用 `--set` 覆盖，镜像仓库和 tag 也是。

## 部署了什么

| 组件 | 说明 | 端口 | 开关 |
|---|---|---|---|
| memos | API 服务（Deployment + Service + ConfigMap + PVC） | 8000 | 始终部署 |
| neo4j | 图库（Neo4j Community） | 7474 / 7687 | `neo4j.enabled`（默认开） |
| qdrant | 向量库 | 6333 / 6334 | `qdrant.enabled`（默认开） |
| redis | 调度器队列 | 6379 | `redis.enabled`（默认开，无密码） |
| ingress | 可选入口 | — | `ingress.enabled`（默认关） |

依赖默认开启，`helm install` 出来就是一套自洽可用的环境（`MEMSCHEDULER_USE_REDIS_QUEUE` 默认为 `true`，没有 redis 时应用会退回在容器里 spawn `redis-server`，而镜像里没有那个二进制，所以 redis 必须有个着落）。**接已有中间件时把对应的 `enabled` 设成 `false`，再用 `--set memos.env.*` 指过去**，chart 就不会创建它们，也不会加等待它们的 initContainer。

**只部署 memos 本体**（复用集群里已有的中间件、嵌入与重排服务）时，把 `neo4j.enabled` / `qdrant.enabled` / `redis.enabled` 设为 `false`，再用 `--set memos.env.*` 指过去；`values-example.yaml` 是自带依赖的完整一套示例。


## 前置条件：镜像必须自足

这个 chart 假设镜像里**已经有完整的代码和已注册的插件**。`memos.env.MEMOS_ENABLED_PLUGINS` 只是点名，插件能不能被加载取决于镜像里有没有本包的 `dist-info`——`entry_points` 是从那里读的。

仓库根目录的 `Dockerfile` 已经处理了两处，用别的 Dockerfile 构建时要自己确认：

1. **`pip install --no-deps .`**。没有 `dist-info` 时 `entry_points` 读不到 `[project.entry-points."memos.plugins"]`，`MEMOS_ENABLED_PLUGINS` 里写了名字也不会有任何插件被加载，且**不报错**。运行时代码仍从 `/app/src` 加载（`PYTHONPATH`）。
2. **`schedule`**。它在 `pyproject.toml` 的 `tree-mem` extra 里，而 reorganizer 在 `reorganize` 路径上会 `import schedule`。线上镜像曾经缺这个包，靠把一个 31 KB 的 `schedule/__init__.py` 通过 ConfigMap 挂进 `site-packages` 来绕过；`docker/requirements.txt` 现在已补上。

顺带一提，**`numpy` 不是可选项**：`organize/reorganizer.py:10`、`retrieve/bm25_util.py:3`、`retrieve/retrieve_utils.py:7` 三处都是模块级无守卫的 `import numpy as np`（`bm25_util.py` 同一处还模块级 import 了 sklearn，连带 scipy）。只有 `reranker/cosine_local.py:16` 那处带 `try` 守卫。

镜像没做到这两点时仍可部署，但要用 `extraVolumes` / `extraVolumeMounts` 把缺失的文件或元数据挂回来。

## 快速开始

```bash
# 单机完整一套（自带 neo4j / qdrant，chart 自建 Secret）
helm install memos deploy/helm -n memos --create-namespace -f deploy/helm/values-example.yaml
```

## 用 --set 覆盖

镜像仓库和 tag 是最常改的两个：

```bash
helm upgrade --install memos deploy/helm -n graphmemory-prod \
  --set memos.image.repository=example.invalid/gyq/memos \
  --set memos.image.tag=dumemos-20261026-2b334550
```

其余常用的：

| 需求 | 参数 |
|---|---|
| 拉私有仓库 | `--set global.imagePullSecrets[0].name=<secret>` |
| 让对象名与线上一致 | `--set fullnameOverride=memos-original` |
| 改副本数 / 资源 | `--set memos.replicaCount=2`、`--set memos.resources.limits.memory=32Gi` |
| 改任意环境变量 | `--set memos.env.MOS_TOP_K=100` |
| 关掉某个插件 | `--set memos.env.MEMOS_ENABLED_PLUGINS=dream` |
| 调 stdout 日志级别 | `--set memos.env.MEMOS_LOG_LEVEL=DEBUG` |
| 换数据盘 | `--set memos.persistence.size=100Gi` |
| 用已有 PVC | `--set memos.persistence.existingClaim=<pvc>` |
| 固定 NodePort | `--set memos.service.type=NodePort --set memos.service.nodePort=8976` |
| 换 initContainer 镜像 | `--set memos.initContainerImage=<内网地址>/busybox:1.36` |
| 关探针（调试用） | `--set memos.probes.liveness.enabled=false` |

`--set` 里带方括号的键在 zsh 下要加引号，否则会被当 glob：

```bash
--set 'global.imagePullSecrets[0].name=example-registry'
```

参数一多就不适合 `--set` 了，写一个覆盖用的 values 文件更清楚：

```bash
helm upgrade --install memos deploy/helm -n graphmemory-prod -f my-values.yaml
```

## 接外部 Neo4j / Qdrant / Redis

```bash
helm upgrade --install memos deploy/helm -n graphmemory-prod \
  --set neo4j.enabled=false --set qdrant.enabled=false --set redis.enabled=false \
  --set memos.env.NEO4J_URI=bolt://memos-original-neo4j:7687 \
  --set memos.env.QDRANT_HOST=memos-original-qdrant \
  --set memos.env.MEMSCHEDULER_REDIS_HOST=memos-original-redis
```

依赖关掉后，`memos.env` 里对应的值原样生效；依赖开着时，chart 会用 in-cluster 的 Service 名覆盖 `NEO4J_URI` / `QDRANT_HOST` / `MEMSCHEDULER_REDIS_HOST`，不用手写。

## 敏感值

两条路，选一条：

**用已有的 Secret**（生产环境走这条，四个敏感值分散在两个 Secret 里）：

```yaml
memos:
  secretRefs:
    - envName: OPENAI_API_KEY
      secretName: memos-original-api-secret
      key: openai-api-key
    - envName: MEMRADER_API_KEY
      secretName: memos-original-api-secret
      key: memrader-api-key
    - envName: NEO4J_PASSWORD
      secretName: memos-original-api-secret
      key: neo4j-password
    - envName: MEMSCHEDULER_REDIS_PASSWORD
      secretName: memos-original-redis-secret
      key: redis-password
```

**让 chart 建一个**：`memos.secret.create=true` 加 `memos.secret.data`。注意 `values.yaml` 会随 release 一起存进集群的 Secret 里，`secret.data` 不适合放生产密钥。

**整份引用一个外部管理的 Secret**：`memos.secret.name=<secret>`（`create` 留 `false`）。它会作为 `envFrom.secretRef` 注入，该 Secret 的所有 key 都会变成环境变量。`create=true` 且 `name` 指向一个已存在的对象时要小心——Helm 会用 `secret.data` 整份替换它，没列出的 key 会被抹掉，`data` 为空时 chart 会直接 `fail` 拦下。

三条路径的注入顺序是 `configMapRef` → Secret（自建或 `secret.name` 指定的）→ `extraEnvFrom` → `env`（`secretRefs` 与 `extraEnv`），**后者覆盖前者**。用 `extraEnvFrom` 塞了与 ConfigMap 同名的 key 会静默覆盖。

## 额外挂载

镜像里缺文件或元数据时，用这两个逃生口，不必改模板：

```yaml
memos:
  extraVolumes:
    - name: extra-file
      configMap:
        name: my-extra-file
  extraVolumeMounts:
    - name: extra-file
      mountPath: /app/src/memos/extra.py
      subPath: extra.py
      readOnly: true
```

优先把文件打进镜像；这两个入口只用于临时补齐。

## 已知的坑

- **`subPath` 挂载不热更新。** 改了 `extraVolumes` 指向的 ConfigMap 之后必须重启 Pod，否则容器里还是旧文件。
- **`neo4j.auth.password` 默认是占位符，必须改。** 留空会直接 `fail`（`neo4j.auth.enabled is true but neo4j.auth.password is empty`）。
- **`redis.enabled=true` 必须给密码。** `redis.auth.enabled` 为真而 `redis.auth.password` 为空时 chart 会直接 `fail`，避免起一个空密码的 Redis。给了密码之后 chart 会自动把 `MEMSCHEDULER_REDIS_PASSWORD` 从它自己建的 Secret 注入到 memos 容器，不用手写。
- **必须有 `startupProbe`，别删。** 应用从容器起到 Ready 实测要 **84 秒**（import numpy/sklearn/transformers 再连 Neo4j/Qdrant），这期间 8000 端口还没监听。liveness 是 `initialDelaySeconds: 30` + `periodSeconds: 10`、`failureThreshold` 默认 3，也就是 60 秒就把容器杀掉——没有 startupProbe 兜着就会 CrashLoopBackOff，永远起不来。chart 默认给 60×5 = 300 秒宽限（与线上一致）。`startup.enabled=false` 只在调试时用。
- **升级到本 chart 需要重建 Deployment，不能原地更新。** `spec.selector` 在 apps/v1 下不可变；现有对象的 selector 与 chart 渲染结果对不上时，`helm upgrade` 与 `kubectl apply` 都会报 `field is immutable`。重建窗口约 **2～3 分钟**（Pod 创建到 Ready 实测 106 秒 + 新镜像在节点上的拉取时间）。
- **`memos.env` 里值为空的键不会渲染。** 需要「设成空字符串」而不是「不设置」时，用 `memos.extraEnv`。
- **`MEMOS_LOG_LEVEL` 默认是 `INFO`**，与线上一致。注意 INFO 会把含用户记忆正文的日志抬到 stdout（见仓库 CHANGELOG 的日志瘦身一节），不需要就设成 `WARNING` 或空串。
- **`MOS_EMBEDDER_API_BASE` 与 `MOS_RERANKER_URL` 默认指向 `memos-emb-server` / `memos-rerank-server`**，这两个服务不由 chart 创建。没有它们时默认装出来的 memos 无法嵌入与重排，要 `--set memos.env.MOS_RERANKER_BACKEND=cosine_local` 之类指到别处。
- **Helm 不会接管已有的手写资源。** `helm upgrade --install` 落到已有的手写对象上会失败，报错形如：
  ```
  ConfigMap "memos-original-api" ... exists and cannot be imported into the current release:
  invalid ownership metadata; label validation error: missing key "app.kubernetes.io/managed-by";
  annotation validation error: missing key "meta.helm.sh/release-name"
  ```
  这是 Helm 的既有行为：先给对象打上所有权标记，或者删掉后交给 chart 重建。
- **改 `Chart.yaml` 的 `appVersion` 不会破坏升级。** Deployment 的 `spec.selector` 在 apps/v1 下不可变，所以 selector 只用 `name` + `instance`，`appVersion` 只进 metadata 与 pod labels（见 `_helpers.tpl` 的 `memos.selectorLabels`）。不要把它挪进 selector。
