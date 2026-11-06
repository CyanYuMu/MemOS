# MemOS Production Dockerfile
# Multi-stage build with optimizations

# 基础镜像。默认走内网 registry 镜像站：docker.io 在内网时通时不通，
# 而 registry-mirror 上的 python:3.11-slim 与 Docker Hub 是同一个 digest
# （sha256:e88e9763f943…），换过去不改变镜像内容。
# 有外网的环境可以 --build-arg BASE_IMAGE=python:3.11-slim 覆盖。
ARG BASE_IMAGE=example.invalid/python:3.11-slim

# Stage 1: Install dependencies
FROM ${BASE_IMAGE} AS builder

# PyPI 源。默认走国内镜像——直连 pypi.org 实测只有 27-80 kB/s，装一遍 130 个包
# 要半小时以上，numpy 一个 16.9 MB 的包就下了 20 分钟。
# 内网有私有源就用 --build-arg PIP_INDEX_URL=<内网地址> 覆盖。
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    build-essential \
    libffi-dev \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY docker/requirements.txt .
RUN pip install --upgrade pip -i "$PIP_INDEX_URL" && \
    pip install --prefix=/install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

# Stage 2: Runtime image
ARG BASE_IMAGE=example.invalid/python:3.11-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 memos

WORKDIR /app

ENV HF_ENDPOINT=https://huggingface.co
ENV PYTHONPATH=/app/src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=builder /install /usr/local
COPY src/ ./src
COPY docker/ ./docker

# 装上本包自身。没有 dist-info 时 importlib.metadata.entry_points() 读不到
# [project.entry-points."memos.plugins"]，MEMOS_ENABLED_PLUGINS 里写了 dream /
# hallucination_guard 也不会有任何插件被加载（静默失效）。
# --no-deps：依赖已由上面的 requirements.txt 装好，这一步只为拿元数据。
# 运行时代码仍从 /app/src 加载（PYTHONPATH）。
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps -i "$PIP_INDEX_URL" . && rm -f pyproject.toml README.md

# 把 gpt2 分词器缓存打进镜像。SentenceChunker 启动时要通过 chonkie 加载 gpt2，
# 而集群没有外网：缓存缺失会直接 `ValueError: Tokenizer not found`，容器起不来。
# 线上 upstream 镜像也是这么做的（tag 后缀 -hf-cache，缓存同样是一份 models--gpt2）。
ENV HF_HOME=/app/.cache/huggingface
RUN for i in 1 2 3; do \
        python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('gpt2')" && break; \
        echo "tokenizer download failed (attempt $i), retrying"; sleep 5; \
        [ "$i" = "3" ] && exit 1; \
    done
# 缓存齐了就不该再联网：线上容器里既没配 HF_HOME 也没联网，靠的就是 HF_HUB_OFFLINE=1。
# 不设这一条，启动阶段会卡在 HF 的 5 次重试上（每次 10s 超时），startupProbe 直接打穿。
ENV HF_HUB_OFFLINE=1

RUN chown -R memos:memos /app

USER memos

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "memos.api.server_api:app", "--host", "0.0.0.0", "--port", "8000"]
