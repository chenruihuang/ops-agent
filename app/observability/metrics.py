"""Prometheus 指标定义。

为什么这里允许在没有 prometheus_client 时降级成空实现：
缓存管理器和评估脚本都会复用这些指标。开发机上跑单测、跑离线脚本时
不该被迫装一整套可观测依赖。这是一个刻意的取舍，不是偷懒。
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
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
# HTTP 层的请求计数 —— 与 llm_requests_total 刻意分开：语义缓存命中时
# 不发生 LLM 调用，命中率 75% 的压测里用 llm_requests_total 当"请求数"，
# 顶行会显示"无流量"，而 nginx 明明刚转发了一千多条请求（实测踩过）。
CHAT_REQUESTS = _counter(
    "chat_requests_total",
    "chat 请求次数（HTTP 层，含缓存命中）",
    ("result",),                  # ok | error | aborted（客户端提前断开）
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
# 工具调用三条指标。`source` 是 mcp / cache / mock / none —— 降级发生时
# 这里能立刻看出"结果到底从哪来"，而不是只看成功率自我安慰。
TOOL_CALLS_TOTAL = _counter(
    "tool_calls_total",
    "工具调用次数（按来源与结果）",
    ("tool", "source", "result"),
)
TOOL_CALL_SECONDS = _histogram(
    "tool_call_seconds",
    "工具调用耗时（按来源：mcp 慢、cache 快，混在一起看会误导）",
    ("tool", "source"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0),
)
TOOL_RETRIES_TOTAL = _counter(
    "tool_retries_total",
    "工具调用重试次数（dependency = 熔断器名）",
    ("dependency",),
)
TOOL_DEGRADED_TOTAL = _counter(
    "tool_degraded_total",
    "工具降级跳数（哪一跳、为什么跳过）",
    ("stage", "reason"),
)

# ---------------- 安全沙箱 ----------------
# `decision` 取值是**决定**，不是结果：allow（只读直接放行）/ pending（挂起等确认）
# / approved（确认后执行）/ rejected（用户拒绝）/ denied（策略拒绝，连确认都不给）
# / invalid_grant（token 过期、已用过、或工具名不匹配）。
#
# 为什么把 denied 和 rejected 分开：两者在面板上都表现为"没执行"，
# 但处置完全不同 —— denied 是有人拿越权参数在探边界（要查谁在调），
# rejected 是人主动点了"不执行"（正常流程）。
SANDBOX_DECISIONS = _counter(
    "sandbox_decisions_total",
    "沙箱决策次数（allow/pending/approved/rejected/denied/invalid_grant）",
    ("decision", "risk"),
)
# 从"挂起"到"人点确认"的时长。它衡量的是**人**，不是系统：
# P95 几十分钟说明确认请求根本没人看，链路设计得再快也没用。
SANDBOX_CONFIRM_SECONDS = _histogram(
    "sandbox_confirm_seconds",
    "待确认动作从挂起到被确认/拒绝的时长",
    ("decision",),
    buckets=(1, 5, 15, 30, 60, 300, 900, 3600),
)
# 在册的写/危险工具数量（启动时 set 一次）。
#
# 为什么这个 gauge 比"决策计数"更重要：counter 只有在**发生过**动作后才存在，
# 于是"闸门被绕过（risk 标成 read）"和"还没人做过写操作"在面板上完全一样 ——
# 都是 noValue。有了这个数就能对照：**在册写工具 > 0，而 pending 一直是 0**，
# 说明闸门没接上。这类失效不报错、不影响功能，只影响安全，最容易漏。
SANDBOX_WRITE_TOOLS = _gauge(
    "sandbox_write_tools",
    "在册的写/危险工具数量（>0 但 pending 恒为 0 = 闸门没接上）",
)

# ---------------- 评估（由 scripts.run_eval --publish 经 POST /admin/eval/metrics 写入）----------------
# 为什么必须走一个 HTTP 端点：`scripts/run_eval.py` 是**独立进程**（宿主机或 eval 容器），
# 它 set 自己的 REGISTRY 没有任何人会抓 —— 面板永远空，而"空"看起来又像"没跑过评估"。
# 只有写进 api 进程的 registry，Prometheus 才抓得到。
CONTEXT_RECALL = _gauge("rag_context_recall", "上下文召回率（自建标注集，文档级）", ("top_k",))
CONTEXT_PRECISION = _gauge("rag_context_precision", "上下文精度（文档级 AP）", ("top_k",))
ANCHOR_HIT_RATE = _gauge("rag_anchor_hit_rate", "标注关键事实命中率（不等于 RAGAS faithfulness）", ("top_k",))
ANSWER_FAITHFULNESS = _gauge("rag_answer_faithfulness", "答案忠实度（需 ragas 判定，当前不产出）")
EVAL_LAST_RUN = _gauge("rag_eval_last_run_timestamp", "最近一次评估的时间戳")


# ---------------- 评估产物持久化 ----------------
# 为什么需要：gauge 是**进程内**的，`docker compose up -d api` 一次就把
# `--publish` 写进去的数清空 —— 面板变空，而人看到的是"评估结果没了"。
# 落到可写目录（trace 目录本来就要写），启动时回填；代价是一次 1KB 读写。
def eval_snapshot_path() -> Path:
    from app.core.config import settings

    return Path(settings.observability_trace_dir) / "eval_metrics.json"


def persist_eval(values: dict[str, float | None], top_k: int) -> Path:
    """把已写入的评估指标落盘（只存**写成功**的项）。返回快照路径。"""
    path = eval_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "top_k": top_k,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "values": {k: v for k, v in values.items() if v is not None},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def restore_eval() -> list[str]:
    """启动时把上次发布的评估指标回填到本进程。返回回填的指标名。"""
    path = eval_snapshot_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    top_k = str(payload.get("top_k", 5))
    values = payload.get("values") or {}
    gauges = {
        "context_recall": CONTEXT_RECALL,
        "context_precision": CONTEXT_PRECISION,
        "anchor_hit_rate": ANCHOR_HIT_RATE,
    }
    restored: list[str] = []
    for name, gauge in gauges.items():
        value = values.get(name)
        if isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
            gauge.labels(top_k=top_k).set(float(value))
            restored.append(name)
    if restored:
        EVAL_LAST_RUN.set(time.time())
    return restored

