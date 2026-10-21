FROM python:3.12-slim

ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# torch 单独一层。它是全镜像里最大的依赖，且极少变；独立成层后改业务代码不会触发重装。
# 国内若拉取缓慢，可改为使用本地 wheel：
#   1) 把 cs-agent 目录下的 torch-*.whl 复制到本项目 vendor/
#   2) 把下面这行换成：pip install /app/vendor/torch-2.12.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url ${TORCH_INDEX} torch

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -i ${PIP_INDEX} -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY loadtest/ ./loadtest/
COPY tests/ ./tests/

ENV HF_ENDPOINT=https://hf-mirror.com

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# 为什么是 1 个 worker 而不是 2：
# prometheus_client 的默认 REGISTRY 是**进程内**的。多个 uvicorn worker 时，
# 每个进程各有一份计数器，而 /metrics 只由处理这次请求的那个 worker 返回 ——
# 于是 Prometheus 每次抓取看到的是"随机某一个 worker 的部分计数"。
# 表现非常隐蔽：计数器时大时小、告警时有时无、大盘曲线像心电图（实测
# 容器内两个 worker 分别记到 3 与 2，Prometheus 只看到 1）。
# 正确做法要么开 multiprocess 模式（PROMETHEUS_MULTIPROC_DIR + MultiProcessCollector），
# 要么 1 worker × N 副本。本项目是无状态服务，**按副本横向扩**更简单，
# 也让"每个容器一个进程"与容器资源限制对齐。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
