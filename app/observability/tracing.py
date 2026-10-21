"""全链路追踪：Langfuse 上报 + 本地 JSONL 明细 + 会话回放。

为什么不是"import langfuse 然后 with trace:" 就完事
---------------------------------------------------
三个约束，缺一个埋点就会变成摆设：

1) **没有 key 也必须能追踪。** Langfuse 在本项目里是可选项（Cloud 或自托管），
   而"埋点到底埋上没埋上"这件事不能依赖云端才能验证。所以本地 JSONL 是
   一等公民：一个 span 一行，trace_id / parent / start_ms / duration_ms 齐全，
   离线就能画时间线、能写断言、能回放整段会话。
   云端是**采样上报**，本地是**全量事实源** —— 这个方向不能反过来。

2) **节点内不能依赖 OTEL 的 contextvar。** LangGraph 把每个节点放在独立的
   asyncio task 里执行，`with span:` 设置的 current span **不会**传播到
   下一个节点。实测（langgraph 1.2.12 + langfuse 5.0.2）：

       node turn     -> get_current_trace_id() = 47cd2454...
       node dispatch -> get_current_trace_id() = None      ← 丢了

   所以子 span 一律走 `root.start_observation(...)` **显式挂到 root**，
   跨 task 安全，不靠隐式上下文。

3) **上报不能进主链路的关键路径。** 实测（连不上时）：Langfuse 客户端
   *构造* 356ms、`flush()` 888ms —— 两个数字都远超一次缓存命中的整条耗时。
   所以：构造放 lifespan（只付一次），flush 只在关机，请求路径上只做
   "建 span 对象"这种微秒级操作。

span 树为什么是平的（root + N 个并列阶段）
------------------------------------------
并行扇出（tools ∥ recall）时，父节点 dispatch 在同一个 superstep 内就结束了；
把子 span 挂到"已结束的父节点"下会触发 OTEL 的
"child started after parent ended" 语义问题。所以：

  · **后端里**所有阶段都挂 root（树深 1，时间线准确）；
  · **逻辑父子关系**写进 `metadata.parent` 与 JSONL 的 `parent` 字段。

回放脚本用的是 JSONL 里的 `parent`，所以本地看得到真实的层级，
而 Langfuse 里看到的是一条完整的 Gantt 时间线。这是取舍，不是疏漏。

TTFT 埋在哪
-----------
TTFT **不是**单独一个数字，它是"generation 的 start 到首 token"的区间：

  · 本地：`generate` span 的 `metadata.ttft_ms` + `first_token_ms`（相对 turn 起点）；
  · Langfuse：generation 的 `completion_start_time`（UI 用它算 Latency to First Token）；
  · Prometheus：`TTFT_SECONDS` 直方图（已有，压测与告警用）。

三处同源同一次测量，不许各算各的 —— 否则报表之间对不上，谁都不敢信。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

# 与 Langfuse 的 trace_id 同形（32 位 hex），两边日志能直接对着看
def _hex_id() -> str:
    return uuid.uuid4().hex


def iso_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(
        timespec="milliseconds"
    )


# =====================================================================
# 本地 JSONL：全量事实源
# =====================================================================
class JsonlSink:
    """一个 span 一行，按本地日期分文件。

    为什么同步写而不是异步队列：
    一行 JSON 约 0.5~2KB，`write + flush` 实测在 0.1ms 量级（见 README 的开销实测），
    而一次请求通常 6~10 个 span。为了省这 1ms 引入队列 + 后台 task，
    换来的是"进程被杀时最后几条丢了"和"关机时 flush 顺序"两类问题 —— 不划算。
    真到写不动的那天（比如 1k QPS），该换的是 OpenTelemetry Collector，
    不是在这里加队列。
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._fh: Any = None
        self._day: str | None = None
        self._error_logged = False

    def _handle(self, epoch: float):
        day = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
        if self._fh is not None and self._day == day:
            return self._fh
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.dir / f"{day}.jsonl", "a", encoding="utf-8")
        self._day = day
        return self._fh

    def emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                fh = self._handle(record.get("start_epoch", time.time()))
                fh.write(line + "\n")
                fh.flush()
        except Exception as exc:  # noqa: BLE001 - 落盘失败绝不能影响主链路
            if not self._error_logged:
                self._error_logged = True
                log.warning("trace jsonl 写入失败（只提示一次）：%s", exc)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:  # noqa: BLE001
                    pass
                self._fh = None


# =====================================================================
# Langfuse 后端：可选的采样上报
# =====================================================================
class LangfuseBackend:
    """Langfuse SDK 的薄包装。

    **构造必须放到 lifespan**：实测 356ms（含 OTEL provider 初始化）。
    若每个请求 new 一个，P50 会直接翻倍还多，而现象看起来像"检索变慢了"。

    **flush 只在关机**：实测连不上时 `flush()` 阻塞 888ms —— 放在请求结束处
    等于给每个请求加一个"等云端确认"的尾延迟，而链路本身只有 1.6s。
    """

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str,
        timeout: float = 5.0,
        release: str = "",
        environment: str = "default",
        flush_at: int = 64,
        flush_interval: float = 2.0,
    ) -> None:
        from langfuse import Langfuse  # 延迟导入：没装也要能跑 JSONL

        self.client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            timeout=timeout,
            release=release or None,
            environment=environment,
            flush_at=flush_at,
            flush_interval=flush_interval,
        )
        self._root: Any = None

    def open_turn(
        self,
        *,
        name: str,
        input: Any,
        metadata: dict[str, Any],
        session_id: str,
        tags: list[str],
    ) -> Any:
        from langfuse import propagate_attributes

        # 为什么不用 `start_as_current_observation`：
        # turn 的生命周期跨越整个 SSE generator，`__exit__` 会在**另一个
        # asyncio Context** 里执行（LangGraph 每个节点一个 task）。实测报错：
        #
        #   Failed to detach context
        #   ValueError: <Token ...> was created in a different Context
        #
        # OTEL 的 contextvar token 只能在创建它的 context 里 reset。
        # 所以 root 用**非 current** 的 `start_observation` + 手动 `end()`：
        # 不碰 contextvar，也就不存在跨 context 的问题。
        #
        # session_id / tags 必须在**创建 root 的那一刻**写进去（Langfuse 的
        # Sessions 视图靠 trace 级属性归组）。`propagate_attributes` 的
        # enter/exit 因此紧挨着放这里 —— 同一 context，进出配对，安全。
        try:
            with propagate_attributes(
                session_id=session_id or None,
                tags=tags or None,
                trace_name=name,
            ):
                self._root = self.client.start_observation(
                    name=name, as_type="span", input=input, metadata=metadata
                )
        except Exception as exc:  # noqa: BLE001 - 归组失败不能影响 span 本身
            log.debug("langfuse 开 trace 失败（降级为无 session 归组）：%s", exc)
            self._root = self.client.start_observation(
                name=name, as_type="span", input=input, metadata=metadata
            )
        return self._root

    def start_span(self, span: "_Span") -> Any:
        """子 span 一律显式挂 root（跨 task 安全，见模块 docstring 第 2 条）。"""
        if self._root is None:
            return None
        try:
            return self._root.start_observation(
                name=span.stage,
                as_type=span.type,
                input=span.input,
                metadata=span.metadata or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse start_observation 失败（%s）：%s", span.stage, exc)
            return None

    def close_turn(
        self, *, output: Any, metadata: dict[str, Any], level: str = "DEFAULT",
        status_message: str | None = None,
    ) -> None:
        try:
            if self._root is not None:
                self._root.update(output=output, metadata=metadata,
                                  level=level, status_message=status_message)
                # `end()` 而不是 cm.__exit__：root 不是 current span，
                # 也就没有要 reset 的 contextvar token（见 open_turn 的说明）。
                self._root.end()
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse root 收尾失败：%s", exc)
        self._root = None

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse flush 失败：%s", exc)

    def close(self) -> None:
        try:
            self.client.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.debug("langfuse shutdown 失败：%s", exc)

    def auth_check(self) -> tuple[bool, str]:
        """给自测脚本用。**必须兜异常**：连不上时 SDK 抛 ConnectError 而不是返回 False。"""
        try:
            ok = bool(self.client.auth_check())
            return ok, "ok" if ok else "auth rejected"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


# =====================================================================
# Span：既能 with，也能手动 end（缓存流要跨函数传递）
# =====================================================================
@dataclass
class _Span:
    trace: "TurnTrace"
    stage: str
    type: str = "span"
    input: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parent: str | None = None
    output: Any = None
    level: str = "DEFAULT"
    status_message: str | None = None
    span_id: str = field(default_factory=_hex_id)
    start_ts: float = field(default_factory=time.time)
    start_rel_ms: float = 0.0
    end_ts: float | None = None
    first_token_ts: float | None = None
    model: str | None = None
    _lf: Any = None
    _closed: bool = False

    # ---- 生命周期 ----
    def __enter__(self) -> "_Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self.level == "DEFAULT":
            self.level = "ERROR"
            self.status_message = self.status_message or f"{exc_type.__name__}: {exc}"
        self.end()
        return False

    def set(self, **kwargs: Any) -> "_Span":
        """就地改字段。`update` 这个名字留给 Langfuse 的 SDK，避免混淆。"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        return self

    def add_meta(self, **kwargs: Any) -> "_Span":
        self.metadata.update(kwargs)
        return self

    def first_token(self) -> None:
        """标记首 token 到达。TTFT 的唯一测量点 —— 三处消费方都读这里。"""
        if self.first_token_ts is None:
            self.first_token_ts = time.time()

    def end(self, **kwargs: Any) -> None:
        if self._closed:
            return
        self._closed = True
        if kwargs:
            self.set(**kwargs)
        self.end_ts = time.time()
        self.trace._emit(self)


# =====================================================================
# Turn：一次请求的根
# =====================================================================
class TurnTrace:
    """一次请求 = 一个 trace。持有 root span、TTFT、以及所有子 span。

    这个对象放在 `AgentState.trace` 里随图流动 —— 和 cache / registry / memory
    一样是**请求级服务句柄**。节点不再各自从全局取，测试也就能注入假的。
    """

    def __init__(
        self,
        *,
        tracer: "Tracer",
        message: str,
        session_id: str = "",
        use_cache: bool = True,
        metadata: dict[str, Any] | None = None,
        sampled: bool = True,
    ) -> None:
        self.tracer = tracer
        self.trace_id = _hex_id()
        self.t0 = time.perf_counter()          # 与 StageTimer 同源，时间线能对齐
        self.start_ts = time.time()
        self.message = message
        self.session_id = session_id or ""
        self.use_cache = use_cache
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.sampled = sampled
        self.spans: list[_Span] = []
        self.ttft_ms: float | None = None
        self.degraded: list[str] = []
        self._lf_backend = tracer.langfuse if sampled else None
        self._lf_root: Any = None
        self._closed = False

        if self._lf_backend is not None:
            self._lf_root = self._lf_backend.open_turn(
                name="turn",
                input={"message": message},
                metadata={"session_id": self.session_id,
                          "use_cache": use_cache, **self.metadata},
                session_id=self.session_id,
                tags=tracer.tags,
            )

    # ---- 建 span ----
    def stage(
        self,
        stage: str,
        *,
        as_type: str = "span",
        input: Any = None,
        parent: str | None = None,
        level: str = "DEFAULT",
        **metadata: Any,
    ) -> _Span:
        """开一个阶段 span。用法两种，语义相同：

            with trace.stage("recall", as_type="retriever", channels=[...]) as sp:
                ...
                sp.set(output={"n": 5})

            sp = trace.stage("generate", as_type="generation", model=...)
            ...
            sp.end(output={"chars": 900})
        """
        span = _Span(
            trace=self,
            stage=stage,
            type=as_type,
            input=input,
            metadata=metadata,
            parent=parent,
            level=level,
            start_rel_ms=round((time.perf_counter() - self.t0) * 1000, 2),
        )
        self.spans.append(span)
        if self._lf_backend is not None and self._lf_root is not None:
            span._lf = self._lf_backend.start_span(span)
        if level != "DEFAULT":
            self.degraded.append(f"{stage}:{metadata.get('reason', level)}")
        return span

    # ---- 收尾 ----
    def mark_ttft(self, ms: float | None = None) -> None:
        """TTFT 的唯一写入点。

        三条路径（闲聊模板 / 缓存命中 / LLM 生成）都调这里，保证
        SSE 的 `stage_ms.first_token`、`done.ttft`、JSONL 的 `turn.ttft_ms`
        与 Prometheus 直方图是**同一次测量**。各算各的迟早对不上。
        """
        if self.ttft_ms is None:
            self.ttft_ms = (
                round(ms, 2) if ms is not None
                else round((time.perf_counter() - self.t0) * 1000, 2)
            )

    def finish(
        self,
        *,
        answer: str = "",
        route: str = "",
        cache: str = "",
        degraded: list[str] | None = None,
        status: str = "ok",
        **extra: Any,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if degraded:
            for item in degraded:
                if item not in self.degraded:
                    self.degraded.append(item)

        total_ms = round((time.perf_counter() - self.t0) * 1000, 2)
        meta = {
            **self.metadata,
            "route": route,
            "cache": cache,
            "degraded": self.degraded,
            "span_count": len(self.spans),
            "ttft_ms": self.ttft_ms,
            **extra,
        }
        # 根记录与子 span 共用一套字段名，回放脚本只认这一套
        record = self._record(
            span_id=self.trace_id,   # 根 span 的 id 就等于 trace_id，省一个字段
            parent=None,
            stage="turn",
            type="agent",
            start_rel_ms=0.0,
            duration_ms=total_ms,
            input={"message": self.message},
            output={"answer": answer},
            metadata=meta,
            level="WARNING" if self.degraded else "DEFAULT",
            status_message=", ".join(self.degraded) or None,
        )
        self.tracer._emit(record)

        if self._lf_backend is not None:
            self._lf_backend.close_turn(
                output={"answer": answer},
                metadata=meta,
                level="WARNING" if self.degraded else "DEFAULT",
                status_message=", ".join(self.degraded) or None,
            )
            self._lf_root = None
        self.tracer._turn_finished()

    # ---- 内部 ----
    def _emit(self, span: _Span) -> None:
        duration = round(((span.end_ts or time.time()) - span.start_ts) * 1000, 2)
        meta = dict(span.metadata)
        if span.parent:
            meta.setdefault("parent", span.parent)
        if span.first_token_ts is not None:
            # 两个基准必须分开命名，否则回放的人一定会算错：
            #   ttft_ms            —— 相对 **turn 起点**（用户视角的"多久看到第一个字"）
            #   first_token_offset —— 相对 **本 span 起点**（生成段内多久出首字）
            # 曾经只记前者，用 span.duration 去比就会得出"TTFT 比生成还慢"的怪结论。
            ttft = round((span.first_token_ts - self.start_ts) * 1000, 2)
            offset = round((span.first_token_ts - span.start_ts) * 1000, 2)
            meta.setdefault("ttft_ms", ttft)
            meta.setdefault("first_token_offset_ms", offset)

        if span._lf is not None:
            try:
                kwargs: dict[str, Any] = {
                    "output": span.output,
                    "metadata": meta or None,
                    "level": span.level,
                    "status_message": span.status_message,
                }
                if span.model:
                    kwargs["model"] = span.model
                if span.first_token_ts is not None:
                    kwargs["completion_start_time"] = datetime.fromtimestamp(
                        span.first_token_ts, tz=timezone.utc
                    )
                span._lf.update(**kwargs)
                span._lf.end()
            except Exception as exc:  # noqa: BLE001
                log.debug("langfuse span 收尾失败（%s）：%s", span.stage, exc)

        self.tracer._emit(self._record(
            span_id=span.span_id,
            parent=span.parent,
            stage=span.stage,
            type=span.type,
            start_rel_ms=span.start_rel_ms,
            duration_ms=duration,
            input=span.input,
            output=span.output,
            metadata=meta,
            level=span.level,
            status_message=span.status_message,
            first_token_ms=(
                round((span.first_token_ts - self.start_ts) * 1000, 2)
                if span.first_token_ts else None
            ),
        ))

    def _record(
        self,
        *,
        span_id: str,
        parent: str | None,
        stage: str,
        type: str,
        start_rel_ms: float,
        duration_ms: float,
        input: Any,
        output: Any,
        metadata: dict[str, Any],
        level: str,
        status_message: str | None,
        first_token_ms: float | None = None,
    ) -> dict[str, Any]:
        return {
            "ts": iso_ts(self.start_ts),
            "start_epoch": self.start_ts,
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent": parent,
            "session_id": self.session_id,
            "stage": stage,
            "type": type,
            "start_ms": start_rel_ms,
            "duration_ms": duration_ms,
            "first_token_ms": first_token_ms,
            "input": input,
            "output": output,
            "metadata": metadata,
            "level": level,
            "status_message": status_message,
            "sampled": self.sampled,
        }


# =====================================================================
# Tracer：进程级单例
# =====================================================================
class Tracer:
    """把"要不要上报"和"埋不埋点"彻底分开。

    `enabled=False` 时不建 span、不写盘，返回的是 `NullTurn` —— 但**编排器
    里的埋点调用一行都不用改**。埋点是代码结构的一部分，不是运行时开关。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        jsonl_dir: str | Path | None = None,
        sample_rate: float = 1.0,
        langfuse: LangfuseBackend | None = None,
        tags: list[str] | None = None,
        release: str = "",
    ) -> None:
        self.enabled = enabled
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self.langfuse = langfuse
        self.tags = list(tags or [])
        self.release = release
        self._jsonl = JsonlSink(jsonl_dir) if (enabled and jsonl_dir) else None
        self._inflight = 0
        self._emit_error_logged = False

    # ---- 开关 ----
    @property
    def sink_description(self) -> str:
        parts = []
        if self._jsonl is not None:
            parts.append(f"jsonl:{self._jsonl.dir}")
        if self.langfuse is not None:
            parts.append("langfuse")
        return " + ".join(parts) or "off"

    def start_turn(
        self,
        *,
        message: str,
        session_id: str = "",
        use_cache: bool = True,
        **metadata: Any,
    ) -> "TurnTrace | NullTurn":
        if not self.enabled:
            return NullTurn()
        # 采样只作用于云端上报：本地 JSONL 永远全量。
        # 反过来（本地也采样）会让"回放某次失败请求"变成碰运气。
        sampled = self.sample_rate >= 1.0 or (
            self.sample_rate > 0 and (uuid.uuid4().int % 10000) < self.sample_rate * 10000
        )
        self._inflight += 1
        return TurnTrace(
            tracer=self,
            message=message,
            session_id=session_id,
            use_cache=use_cache,
            metadata=metadata,
            sampled=sampled,
        )

    def _emit(self, record: dict[str, Any]) -> None:
        """最后一道闸：**任何 sink 的异常都不许冒到调用方**。

        为什么 sink 内部已经 try 了还要再兜一层：埋点代码散在编排器的每个节点里，
        只要有一处例外冒出来，就会顺着 `_Span.__exit__` 把整个节点炸掉 ——
        表现是 SSE 中途断流、`pipeline crashed`，而根因是"磁盘满了"。
        可观测是旁路，旁路的故障必须以"降级为没有 trace"收场。
        """
        if self._jsonl is None:
            return
        try:
            self._jsonl.emit(record)
        except Exception as exc:  # noqa: BLE001
            if not self._emit_error_logged:
                self._emit_error_logged = True
                log.warning("trace 落盘失败（只提示一次，后续静默）：%s", exc)

    def _turn_finished(self) -> None:
        self._inflight = max(0, self._inflight - 1)

    # ---- 收尾 ----
    def flush(self) -> None:
        if self.langfuse is not None:
            self.langfuse.flush()

    def close(self) -> None:
        if self.langfuse is not None:
            self.langfuse.close()
        if self._jsonl is not None:
            self._jsonl.close()


class NullTurn:
    """`enabled=False` 时的替身。接口与 TurnTrace 一致，全部 no-op。"""

    trace_id = ""
    ttft_ms: float | None = None
    spans: list[Any] = []
    sampled = False

    def stage(self, *_args: Any, **_kwargs: Any) -> "_NullSpan":
        return _NullSpan()

    def mark_ttft(self, _ms: float | None = None) -> None:
        return None

    def finish(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _NullSpan:
    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def set(self, **_kwargs: Any) -> "_NullSpan":
        return self

    def add_meta(self, **_kwargs: Any) -> "_NullSpan":
        return self

    def first_token(self) -> None:
        return None

    def end(self, **_kwargs: Any) -> None:
        return None


# =====================================================================
# 装配
# =====================================================================
_TRACER: Tracer | None = None


def _host_reachable(host: str, timeout: float = 1.5) -> tuple[bool, str]:
    """TCP 探活。**只用来快速失败，不代表认证/配额可用。**

    为什么需要这一步：实测 Langfuse 客户端在 host 不可达时，**构造**本身就
    要 10~14 秒（Windows 宿主 + SDK 5.0.1，`timeout=1` 仍会多次重试）；
    容器里同参数是 0.6 秒（5.0.2）。这个差异说明"构造耗时与网络强相关"，
    所以不能假设它总是便宜 —— 启动路径上白等 14 秒，看起来就是"服务起不来"。

    探活通过后再构造，最坏情况多花 1.5 秒；探活失败直接不建 backend，
    本地 JSONL 照常工作。
    """
    import socket
    from urllib.parse import urlparse

    raw = host if "://" in host else f"http://{host}"
    parsed = urlparse(raw)
    hostname = parsed.hostname
    if not hostname:
        return False, f"无法解析 host：{host!r}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            pass
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{hostname}:{port} 不可达（{type(exc).__name__}）"


def _construct_with_timeout(factory, seconds: float) -> tuple[Any, str]:
    """在后台线程里构造，最多等 `seconds` 秒。

    为什么要这一步：实测 Langfuse 客户端**构造**耗时极不稳定 ——
    容器内 SDK 5.0.2 是 0.6s，而 Windows 宿主 5.0.1 上同一份代码
    测到过 10s / 14s / 19s / **51s**（host 可达，参数相同）。
    这类耗时与网络质量强相关，不该出现在启动路径的关键路径上：
    启动慢 51 秒，排查方向会全部指向"服务起不来"，而不是"SDK 在等网络"。

    超时后放弃上报（本地 JSONL 照常），后台线程自己跑完即被丢弃 ——
    它不会再产生 span，因为我们已经不往它上面挂了。
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = factory()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, name="langfuse-init", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return None, f"构造超过 {seconds:.0f}s，放弃上报"
    if "error" in box:
        return None, f"{type(box['error']).__name__}: {box['error']}"
    return box.get("value"), ""


class _SilenceOtelDetachFilter(logging.Filter):
    """吞掉 `opentelemetry.context` 的 "Failed to detach context"。

    2026-10-18 实测证据（容器内 SDK 5.0.2，两次真实请求 + API 核对）：
    - 15 分钟内该 ERROR 只出现 1 次，且**同一时段 Langfuse 云端完整收到 12 条
      observation**（10 阶段 span + trace_doctor 测试 span，EMBEDDING/RETRIEVER/
      GENERATION 类型映射全部正确）—— detach 失败没有影响任何上报；
    - 原理：detach 失败只影响"恢复当前 OTEL context"。本项目的子 span 全部
      **显式挂 root**、从不依赖 current context，所以这个恢复失败对本链路是空操作；
    - 根因在 Langfuse SDK 5.x 内部（asyncio task 间 attach/detach 配对），不在
      本项目代码里。按"确认无害的已知噪音"处理：降为 DEBUG 留痕，不冒 ERROR。

    如果未来某天云端开始缺 span，先把这个过滤摘掉再查 —— 别让过滤器背锅。
    """

    _NEEDLE = "Failed to detach context"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and self._NEEDLE in record.getMessage():
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
            record.msg = "[已知的 SDK 噪音，见 tracing.py 注释] " + str(record.msg)
        return True


def _silence_otel_detach_noise() -> None:
    """挂一次即可（幂等：Filter 不会重复叠加同一个实例）。"""
    logging.getLogger("opentelemetry.context").addFilter(_SilenceOtelDetachFilter())


def build_tracer(*, settings: Any | None = None) -> Tracer:
    """按配置装配。**永远不抛** —— 可观测是锦上添花，不能拖垮启动。"""
    if settings is None:  # pragma: no cover - 正常路径总是从 main 传入
        from app.core.config import settings as _s

        settings = _s

    if not getattr(settings, "observability_enabled", True):
        log.info("tracing disabled (OBSERVABILITY_ENABLED=false)")
        return Tracer(enabled=False)

    backend: LangfuseBackend | None = None
    public_key = (getattr(settings, "langfuse_public_key", "") or "").strip()
    secret_key = (getattr(settings, "langfuse_secret_key", "") or "").strip()
    if getattr(settings, "langfuse_enabled", False) and public_key and secret_key:
        reachable, why = _host_reachable(settings.langfuse_host)
        if not reachable:
            log.warning(
                "Langfuse host %s —— 跳过上报，仅本地 JSONL（%s）",
                why, settings.langfuse_host,
            )
        else:
            init_budget = float(getattr(settings, "langfuse_init_timeout", 15.0))
            backend, err = _construct_with_timeout(
                lambda: LangfuseBackend(
                    public_key=public_key,
                    secret_key=secret_key,
                    host=settings.langfuse_host,
                    timeout=float(getattr(settings, "langfuse_timeout", 5.0)),
                    release=getattr(settings, "app_release", "") or "",
                    environment=getattr(settings, "app_environment", "default") or "default",
                    flush_at=int(getattr(settings, "langfuse_flush_at", 64)),
                    flush_interval=float(getattr(settings, "langfuse_flush_interval", 2.0)),
                ),
                init_budget,
            )
            if backend is None:
                log.error("langfuse 初始化未完成（%s），降级为仅本地 JSONL", err)
            else:
                _silence_otel_detach_noise()
                log.info("langfuse tracing on: %s", settings.langfuse_host)
    elif getattr(settings, "langfuse_enabled", False):
        # 这个分支最容易让人误会"接了 Langfuse 却看不到数据"
        log.warning(
            "LANGFUSE_ENABLED=true 但未配置 PUBLIC/SECRET KEY —— 只落本地 JSONL，不上报云端"
        )

    jsonl_dir = getattr(settings, "observability_trace_dir", "data/traces")
    raw_tags = getattr(settings, "observability_tags", "") or ""
    tags = [t.strip() for t in str(raw_tags).split(",") if t.strip()]
    return Tracer(
        enabled=True,
        jsonl_dir=jsonl_dir or None,
        sample_rate=float(getattr(settings, "observability_sample_rate", 1.0)),
        langfuse=backend,
        tags=tags,
    )


def configure(tracer: Tracer) -> Tracer:
    global _TRACER
    _TRACER = tracer
    return tracer


def get_tracer() -> Tracer:
    """进程级单例。未配置时返回"关闭"的 tracer —— 绝不返回 None，
    否则每个调用点都要写 `if tracer:`，埋点很快就会漏。"""
    global _TRACER
    if _TRACER is None:
        _TRACER = Tracer(enabled=False)
    return _TRACER


_NOOP_SPAN = _NullSpan()


def null_span() -> _NullSpan:
    """给"state 里可能没有 trace"的调用点一个共享的 no-op 替身。

    这样编排器里就不需要写 `if trace is not None:` —— 分支越多，漏埋一处
    的概率越大，而漏埋是**静默**的（trace 里少一段，没人报警）。
    """
    return _NOOP_SPAN


def iter_trace_records(path: str | Path) -> Iterator[dict[str, Any]]:
    """读回放脚本用：按行解析，坏行跳过 —— 日志文件不该因为一行坏了就打不开。"""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
