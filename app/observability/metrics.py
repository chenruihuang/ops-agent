"""Prometheus 指标定义。

为什么这里允许在没有 prometheus_client 时降级成空实现：
缓存管理器和评估脚本都会复用这些指标。开发机上跑单测、跑离线脚本时
不该被迫装一整套可观测依赖。这是一个刻意的取舍，不是偷懒。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

try:  # pragma: no cover - 取决于环境
    from prometheus_client import Counter, Gauge, Histogram

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover
    _HAS_PROMETHEUS = False


class _NoopMetric:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopMetric":
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def dec(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def observe(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    @contextmanager
    def time(self):
        yield


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()):
    if _HAS_PROMETHEUS:
        return Counter(name, doc, labels)
    return _NoopMetric()


def _histogram(name: str, doc: str, labels: tuple[str, ...] = (), buckets=None):
    if _HAS_PROMETHEUS:
        kwargs = {"buckets": buckets} if buckets else {}
        return Histogram(name, doc, labels, **kwargs)
    return _NoopMetric()


def _gauge(name: str, doc: str, labels: tuple[str, ...] = ()):
    if _HAS_PROMETHEUS:
        return Gauge(name, doc, labels)
    return _NoopMetric()


# ---------------- 缓存 ----------------
CACHE_REQUESTS = _counter(
    "cache_requests_total",
    "缓存查询次数",
    ("layer", "result"),          # layer: exact|semantic ; result: hit|miss|rejected
)
CACHE_STORES = _counter("cache_stores_total", "缓存写入次数", ("layer",))
CACHE_LOOKUP_SECONDS = _histogram(
    "cache_lookup_seconds",
    "缓存查询耗时",
    ("layer",),
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)
CACHE_SEMANTIC_SIMILARITY = _histogram(
    "cache_semantic_similarity",
    "语义缓存候选相似度分布 —— 用来标定阈值",
    (),
    buckets=(0.5, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.96, 0.97, 0.98, 0.99, 1.0),
)
CACHE_GUARD_REJECTED = _counter(
    "cache_guard_rejected_total",
    "被护栏拒绝的语义命中次数",
    ("reason",),
)
CACHE_TOKENS_SAVED = _counter(
    "cache_tokens_saved_total",
    "因缓存命中而省下的 LLM token 数（成本指标）",
)

# ---------------- 链路 ----------------
TTFT_SECONDS = _histogram(
    "ttft_seconds",
    "首 token 延迟",
    ("cache", "route"),           # cache: hit|miss
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
)
STAGE_SECONDS = _histogram(
    "stage_seconds",
    "各阶段耗时（用于定位 P95 劣化到底出在哪一段）",
    ("stage",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)
REQUEST_TOTAL = _counter(
    "llm_requests_total",
    "LLM 调用次数",
    ("model", "result"),
)

# ---------------- 高可用 ----------------
CIRCUIT_STATE = _gauge(
    "circuit_breaker_state",
    "熔断器状态：0=closed 1=half_open 2=open",
    ("dependency", "state"),
)
DEGRADED_TOTAL = _counter(
    "degraded_responses_total",
    "降级应答次数",
    ("stage", "reason"),
)

# ---------------- 评估（由 scripts.run_eval 写入）----------------
CONTEXT_RECALL = _gauge("rag_context_recall", "上下文召回率（自建标注集）", ("top_k",))
ANSWER_FAITHFULNESS = _gauge("rag_answer_faithfulness", "答案忠实度（RAGAS）")
EVAL_LAST_RUN = _gauge("rag_eval_last_run_timestamp", "最近一次评估的时间戳")
