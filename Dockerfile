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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
