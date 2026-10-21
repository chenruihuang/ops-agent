"""追踪埋点测试：TTFT、各阶段耗时、会话可回放。

为什么这些断言必须存在
----------------------
埋点是**静默失效**的典型代表：

  · 少埋一个 stage —— 没有任何报错，只是 trace 里少了一段；
  · TTFT 埋错位置（比如在整段生成之后才记）—— 数字看起来正常，
    但它测的是"总耗时"，所有基于它的结论都是错的；
  · 写盘抛异常没人管 —— 请求照样 200，只是再也没有可回放的记录。

上面三种都不会让任何别的用例变红。所以这里钉的不是"函数能跑"，
而是**每条数据的产生位置与相互关系**：
TTFT 必须早于生成结束、turn 必须包住所有 span、session_id 必须每一行都有。

测试不联网、不连 Redis/Qdrant、不加载模型：假 cache / 假 rag + 真 tracer，
写盘落到 tmp_path。Langfuse 用鸭子类型的假后端，只验证"调没调、传了什么"。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.chat import ChatRequest, chat_stream          # noqa: E402
from app.cache.manager import CachedAnswer                # noqa: E402
from app.core.config import settings                      # noqa: E402
from app.observability import tracing                     # noqa: E402
from app.observability.tracing import (                    # noqa: E402
    Tracer,
    configure,
    iter_trace_records,
)
from app.pipeline import rag                              # noqa: E402

PASS = 0
FAIL = 0
API_KEY = settings.api_key.get_secret_value()


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# =====================================================================
# 假依赖（与本文件无关的部分一律不引入网络）
# =====================================================================
class FakeCache:
    def __init__(self, *, hit: CachedAnswer | None = None):
        self.hit = hit
        self.stored: list[CachedAnswer] = []

    async def current_kb_version(self) -> int:
        return 3

    async def lookup(self, query, *, embedding=None):
        if self.hit is not None:
            return self.hit
        return CachedAnswer(answer="", queried=query, source="miss")

    async def get_semantic(self, query, embedding):
        if self.hit is not None and self.hit.source == "semantic":
            return self.hit
        return None

    async def acquire_singleflight(self, query) -> bool:
        return True

    async def release_singleflight(self, query) -> None:
        return None

    async def store(self, query, value, *, embedding=None):
        self.stored.append(value)


class FakeRequest:
    def __init__(self, *, cache=None, tools=None):
        self.app = SimpleNamespace(state=SimpleNamespace(cache=cache, tools=tools))


class FakeLangfuse:
    """鸭子类型的后端：只记录调用，不发网络请求。

    用它而不是真 SDK，是为了让"采样 0 就不上报"这类断言能精确判断 ——
    真 SDK 在无网环境下是静默丢弃，测不出来到底有没有调。
    """

    def __init__(self) -> None:
        self.opened: list[dict] = []
        self.spans: list[str] = []
        self.closed: list[dict] = []
        self.flushed = 0

    def open_turn(self, *, name, input, metadata, session_id, tags):
        self.opened.append({"name": name, "input": input,
                            "session_id": session_id, "tags": tags})
        return SimpleNamespace()

    def start_span(self, span):
        self.spans.append(span.stage)
        return None

    def close_turn(self, *, output, metadata, level="DEFAULT", status_message=None):
        self.closed.append({"output": output, "metadata": metadata, "level": level})

    def flush(self) -> None:
        self.flushed += 1

    def close(self) -> None:
        return None


def fake_settings(**overrides):
    base = dict(
        observability_enabled=True,
        observability_trace_dir="",
        observability_sample_rate=1.0,
        observability_tags="test",
        langfuse_enabled=False,
        langfuse_public_key="",
        langfuse_secret_key="",
        langfuse_host="http://127.0.0.1:9",
        langfuse_timeout=1.0,
        langfuse_init_timeout=1.0,
        langfuse_flush_at=8,
        langfuse_flush_interval=1.0,
        app_release="test",
        app_environment="test",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def install_fakes(monkeypatch, *, chunks=None, generate_raises: bool = False):
    chunks = chunks if chunks is not None else [
        rag.Chunk(doc_id="rb-mysql-conn-pool", chunk_id="rb-mysql-conn-pool#01",
                  text="连接池耗尽处置：先查 processlist",
                  source="rb-mysql-conn-pool", score=0.9),
        rag.Chunk(doc_id="alert-p1-db-conn", chunk_id="alert-p1-db-conn#01",
                  text="连接数超过 max_connections 85% 告警",
                  source="alert-p1-db-conn", score=0.8, recall_source="bm25"),
    ]

    async def fake_embed_one(text):
        return [0.1] * 8

    async def fake_recall_detailed(query, *, embedding=None, top_k=None, fusion=None):
        return rag.RecallOutcome(chunks=list(chunks),
                                 channels={"vector": 2, "bm25": 1}, fused=True)

    async def fake_rerank(query, items, top_k=None, **kwargs):
        return list(items)[: (top_k or 5)]

    async def fake_generate(question, items, *, tool_context="", history=""):
        if generate_raises:
            raise RuntimeError("LLM 连接超时")
        for piece in ("根据", "资料", "回答完毕"):
            yield piece

    monkeypatch.setattr("app.pipeline.orchestrator.embed_one", fake_embed_one)
    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall_detailed)
    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", fake_rerank)
    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)


def install_tracer(tmp_path: Path, *, sample_rate: float = 1.0,
                   backend=None, enabled: bool = True) -> Tracer:
    tracer = Tracer(
        enabled=enabled,
        jsonl_dir=str(tmp_path) if enabled else None,
        sample_rate=sample_rate,
        langfuse=backend,
        tags=["test"],
    )
    configure(tracer)
    return tracer


def read_records(tmp_path: Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(Path(tmp_path).glob("*.jsonl")):
        records.extend(iter_trace_records(path))
    return records


async def collect(response) -> list[str]:
    raw: list[str] = []
    async for chunk in response.body_iterator:
        raw.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return raw


async def run_chat(message: str, *, cache=None, session_id: str | None = None,
                   use_cache: bool = False) -> list[dict]:
    resp = await chat_stream(
        ChatRequest(message=message, use_cache=use_cache, session_id=session_id),
        FakeRequest(cache=cache),
        x_api_key=API_KEY,
    )
    raw = await collect(resp)
    events = []
    for chunk in raw:
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body != "[DONE]":
                events.append(json.loads(body))
    return events


# =====================================================================
# 用例
# =====================================================================
def test_build_without_keys_keeps_local_sink(tmp_path: Path) -> None:
    print("没有 key：退回本地 JSONL，且明确说清楚没上报")
    tracer = tracing.build_tracer(settings=fake_settings(
        observability_trace_dir=str(tmp_path)))
    check("无 key 时不创建 langfuse 后端", tracer.langfuse is None,
          f"实际 {tracer.langfuse}")
    check("本地 JSONL 仍然开启", tracer.sink_description.startswith("jsonl:"),
          tracer.sink_description)
    check("总开关关闭时不建任何 sink",
          tracing.build_tracer(settings=fake_settings(
              observability_enabled=False)).sink_description == "off")
    tracer.close()


def test_unreachable_host_degrades_to_local(tmp_path: Path) -> None:
    print("Langfuse host 不可达：上报关掉、本地照常、启动不被拖住")
    started = time.perf_counter()
    tracer = tracing.build_tracer(settings=fake_settings(
        observability_trace_dir=str(tmp_path),
        langfuse_enabled=True,
        langfuse_public_key="pk-lf-fake",
        langfuse_secret_key="sk-lf-fake",
        langfuse_host="http://127.0.0.1:9",
    ))
    elapsed = time.perf_counter() - started
    check("不可达时不创建 backend", tracer.langfuse is None, str(tracer.langfuse))
    check("本地 JSONL 仍然可用", tracer.sink_description.startswith("jsonl:"),
          tracer.sink_description)
    # 探活上限 1.5s + 余量；真正要防的是"构造把启动卡住几十秒"
    check("快速失败（< 5s）", elapsed < 5.0, f"{elapsed:.1f}s")
    tracer.close()


def test_construct_timeout_gives_up() -> None:
    print("构造超时保护：可选依赖不许拖住启动")
    started = time.perf_counter()
    value, err = tracing._construct_with_timeout(lambda: (time.sleep(2), "ok")[1], 0.3)
    elapsed = time.perf_counter() - started
    check("超时返回 None", value is None, str(value))
    check("原因可读", "构造超过" in err, err)
    check("没有等满 2 秒", elapsed < 1.2, f"{elapsed:.2f}s")


def test_stages_and_ttft(tmp_path: Path, monkeypatch) -> None:
    print("一次 rag 请求：阶段齐全、TTFT 早于生成结束、turn 包住所有 span")
    install_fakes(monkeypatch)
    # .env 里 RERANK_ENABLED=false（阶段 0.1 的要求），此时本来就没有 rerank span。
    # 这里显式打开：否则这条断言会跟着 .env 变红/变绿 —— 那种"假通过"比失败更糟。
    monkeypatch.setattr(settings, "rerank_enabled", True)
    tracer = install_tracer(tmp_path)
    try:
        events = asyncio.run(run_chat("如何排查 502 与 504", session_id="sess-t1"))
        done = next((e for e in events if e.get("type") == "done"), {})
        check("请求本身正常收尾（埋点没有改变协议）", bool(done), str(events[-1:]))

        records = read_records(tmp_path)
        stages = [r["stage"] for r in records]
        turn = next((r for r in records if r["stage"] == "turn"), None)
        check("写出 turn 记录", turn is not None, f"stages={stages}")
        for stage in ("route", "embed", "cache_lookup", "recall", "generate"):
            check(f"埋到 {stage}", stage in stages, f"stages={stages}")
        check("rerank 是独立子 span（它是最贵的一环）", "rerank" in stages, f"stages={stages}")

        if turn is None:
            return
        turn_ms = turn["duration_ms"]
        orphan = [r["stage"] for r in records
                  if r["stage"] != "turn" and r["duration_ms"] > turn_ms + 1]
        check("没有比 turn 还长的 span（否则说明 span 跨请求泄漏）",
              not orphan, f"超长 span={orphan}")
        check("session_id 每一行都带（会话回放的前提）",
              all(r["session_id"] == "sess-t1" for r in records),
              f"{set(r['session_id'] for r in records)}")
        # 闭环：用户手里只有 meta 帧，凭它要能定位到落盘的那条 trace。
        # 这条断言曾经缺失，于是 trace_id 只存在于文件里 —— 回放得先猜会话名。
        meta_frame = next((e for e in events if e.get("type") == "meta"), {})
        check("meta 帧带 trace_id（能直接拿去回放/搜 Langfuse）",
              bool(meta_frame.get("trace_id")), str(meta_frame))
        check("meta.trace_id 与落盘的 trace_id 一致（闭环）",
              meta_frame.get("trace_id") == turn["trace_id"],
              f"{meta_frame.get('trace_id')} != {turn['trace_id']}")
        check("路由决策写进 trace（可解释）",
              (turn.get("metadata") or {}).get("route") == "rag",
              str((turn.get("metadata") or {}).get("route")))

        # TTFT：turn 与 generate 两处必须同源
        ttft = (turn.get("metadata") or {}).get("ttft_ms")
        gen = next((r for r in records if r["stage"] == "generate"), None)
        check("turn 记了 ttft_ms", isinstance(ttft, (int, float)) and ttft > 0, str(ttft))
        check("generate span 记了首 token 位置",
              gen is not None and isinstance(gen.get("first_token_ms"), (int, float)),
              str(gen and gen.get("first_token_ms")))
        if ttft and gen and gen.get("first_token_ms"):
            delta = abs(gen["first_token_ms"] - ttft)
            check("两处 TTFT 同源（差 < 20ms）", delta < 20,
                  f"turn={ttft} generate={gen['first_token_ms']}")
            # 注意两个基准不能混：first_token_ms 相对 **turn 起点**（88ms 很正常），
            # 而 duration_ms 是 **generate span 自己**的长度。拿前者比后者会得出
            # "TTFT 比生成还慢"的怪结论 —— 那正是这条断言第一版写错的地方。
            offset = (gen.get("metadata") or {}).get("first_token_offset_ms")
            check("首 token 落在 generate span 内（生成段内的真实首字延迟）",
                  isinstance(offset, (int, float)) and 0 <= offset <= gen["duration_ms"] + 1,
                  f"offset={offset} duration={gen['duration_ms']}")
            check("TTFT 早于整轮结束（不是把总耗时当 TTFT）",
                  ttft <= turn_ms, f"ttft={ttft} total={turn_ms}")
        check("turn 的 ttft 不超过 total",
              bool(ttft) and ttft <= turn_ms + 1, f"ttft={ttft} total={turn_ms}")
        check("检索来源可回放（top_sources 非空）",
              bool((next((r for r in records if r["stage"] == "recall"), {})
                    .get("output") or {}).get("top_sources")), "recall.output.top_sources")
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_cache_hit_has_no_generate(tmp_path: Path, monkeypatch) -> None:
    print("缓存命中：TTFT 极小、没有 generate span、cache 标注可回放")
    install_fakes(monkeypatch)
    hit = CachedAnswer(answer="连接池耗尽先查 processlist 再扩容", queried="怎么排查",
                       route="rag", source="semantic", similarity=0.9721)
    tracer = install_tracer(tmp_path)
    try:
        asyncio.run(run_chat("怎么排查", cache=FakeCache(hit=hit),
                             session_id="sess-hit", use_cache=True))
        records = read_records(tmp_path)
        turn = next((r for r in records if r["stage"] == "turn"), {})
        stages = [r["stage"] for r in records]
        check("命中路径不产生 generate span（没调 LLM）",
              "generate" not in stages, f"stages={stages}")
        check("cache 命中层级进 trace", (turn.get("metadata") or {}).get("cache") == "semantic",
              str((turn.get("metadata") or {}).get("cache")))
        check("similarity 进 trace（语义命中要能解释）",
              (turn.get("metadata") or {}).get("similarity") == 0.9721,
              str((turn.get("metadata") or {}).get("similarity")))
        ttft = (turn.get("metadata") or {}).get("ttft_ms")
        check("命中路径的 TTFT 也被记录", isinstance(ttft, (int, float)) and ttft >= 0, str(ttft))
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_generate_failure_is_marked(tmp_path: Path, monkeypatch) -> None:
    print("生成失败：trace 标 ERROR，且请求仍是降级而非 500")
    install_fakes(monkeypatch, generate_raises=True)
    tracer = install_tracer(tmp_path)
    try:
        events = asyncio.run(run_chat("如何排查 502", session_id="sess-err"))
        kinds = [e.get("type") for e in events]
        check("仍然有 error 帧（协议未变）", "error" in kinds, str(kinds[-4:]))
        records = read_records(tmp_path)
        gen = next((r for r in records if r["stage"] == "generate"), {})
        check("generate span 标记 ERROR", gen.get("level") == "ERROR", str(gen.get("level")))
        check("失败原因写进 trace（不是空的）",
              bool(gen.get("status_message")), str(gen.get("status_message")))
        turn = next((r for r in records if r["stage"] == "turn"), {})
        check("turn 仍是 warning 级别（降级可见）",
              turn.get("level") in ("WARNING", "ERROR"), str(turn.get("level")))
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_sampled_out_still_local(tmp_path: Path, monkeypatch) -> None:
    print("采样率 0：云端不报，本地全量 —— 回放不能靠运气")
    install_fakes(monkeypatch)
    backend = FakeLangfuse()
    tracer = install_tracer(tmp_path, sample_rate=0.0, backend=backend)
    try:
        asyncio.run(run_chat("如何排查 504", session_id="sess-s0"))
        check("采样 0 时没有开 turn", not backend.opened, str(backend.opened))
        records = read_records(tmp_path)
        check("本地 JSONL 照样有完整记录", len(records) >= 5, f"{len(records)} 条")
        check("记录被标记为未采样", all(r["sampled"] is False for r in records))
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_langfuse_called_when_sampled(tmp_path: Path, monkeypatch) -> None:
    print("有后端且采样命中：session_id 与阶段都传给 Langfuse")
    install_fakes(monkeypatch)
    backend = FakeLangfuse()
    tracer = install_tracer(tmp_path, sample_rate=1.0, backend=backend)
    try:
        asyncio.run(run_chat("如何排查 502", session_id="sess-lf"))
        check("开了 turn", len(backend.opened) == 1, str(backend.opened))
        check("session_id 传给后端（会话回放在云端的唯一入口）",
              backend.opened and backend.opened[0]["session_id"] == "sess-lf",
              str(backend.opened))
        check("阶段 span 也上报了", "generate" in backend.spans, str(backend.spans))
        check("turn 被收尾（否则 Langfuse 里是悬挂 trace）",
              len(backend.closed) == 1, str(backend.closed))
        meta = (backend.closed[0].get("metadata") or {}) if backend.closed else {}
        check("上报的 metadata 带 ttft_ms", "ttft_ms" in meta, str(sorted(meta)))
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_sink_failure_does_not_break_request(tmp_path: Path, monkeypatch) -> None:
    print("写盘炸了：请求照常完成（可观测不能反过来拖垮主链路）")
    install_fakes(monkeypatch)
    tracer = install_tracer(tmp_path)

    def boom(_record):
        raise OSError("disk full")

    monkeypatch.setattr(tracer._jsonl, "emit", boom)
    try:
        events = asyncio.run(run_chat("如何排查 502", session_id="sess-boom"))
        kinds = [e.get("type") for e in events]
        check("仍然收到完整帧序列", "meta" in kinds and "done" in kinds, str(kinds[:3]))
        check("没有异常冒泡", kinds[-1] == "done",
              f"末帧={kinds[-1] if kinds else None}")
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_disabled_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    print("总开关关闭：不建 span、不落盘、请求不受影响")
    install_fakes(monkeypatch)
    tracer = install_tracer(tmp_path, enabled=False)
    try:
        events = asyncio.run(run_chat("如何排查 502", session_id="sess-off"))
        check("请求正常", any(e.get("type") == "done" for e in events))
        check("没有产生任何文件", not list(Path(tmp_path).glob("*.jsonl")),
              str(list(Path(tmp_path).glob("*.jsonl"))))
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_replay_groups_by_trace(tmp_path: Path, monkeypatch) -> None:
    print("回放：同一 session 的两轮能按 trace 分开、按时间排序")
    install_fakes(monkeypatch)
    tracer = install_tracer(tmp_path)
    try:
        asyncio.run(run_chat("第一个问题", session_id="sess-replay"))
        asyncio.run(run_chat("第二个问题", session_id="sess-replay"))
        records = read_records(tmp_path)
        by_trace: dict[str, list[dict]] = {}
        for r in records:
            by_trace.setdefault(r["trace_id"], []).append(r)
        check("产生 2 个 trace（一轮一个）", len(by_trace) == 2, f"{len(by_trace)}")
        check("每个 trace 都有 turn 收尾",
              all(any(x["stage"] == "turn" for x in rows) for rows in by_trace.values()),
              str({k: [x["stage"] for x in v] for k, v in by_trace.items()}))
        ordered = all(
            [x["start_ms"] for x in sorted(rows, key=lambda r: r["start_ms"])]
            == sorted(x["start_ms"] for x in rows)
            for rows in by_trace.values()
        )
        check("start_ms 可用于排序画时间线", ordered)
        sids = {r["session_id"] for r in records}
        check("两轮共享同一个 session_id（会话可串起来）", sids == {"sess-replay"}, str(sids))
    finally:
        tracer.close()
        configure(Tracer(enabled=False))


def test_otel_detach_noise_is_silenced(tmp_path: Path, monkeypatch) -> None:
    """opentelemetry.context 的 detach ERROR 应被降为 DEBUG，其余日志不受影响。

    证据见 tracing.py::_SilenceOtelDetachFilter 的 docstring：同一时段云端完整收到
    12 条 observation —— 这是"确认无害的已知 SDK 噪音"，不是被掩盖的故障。

    直接调 filter(record) 而不是走 logger 管线：test_images / test_indexing 在
    模块导入时 `logging.disable(CRITICAL)`（全局副作用），管线测试会被它静默吞掉。
    """
    import logging as _logging

    flt = tracing._SilenceOtelDetachFilter()
    mk = lambda lvl, msg: _logging.LogRecord(  # noqa: E731
        "opentelemetry.context", lvl, "where.py", 1, msg, None, None
    )

    r1 = mk(_logging.ERROR, "Failed to detach context\nValueError: Token ...")
    check("detach ERROR 通过过滤器（不被丢弃）", flt.filter(r1) is True)
    check("detach ERROR 被降为 DEBUG", r1.levelno == _logging.DEBUG, str(r1.levelno))
    check("降级时留下可检索的标记", "SDK 噪音" in r1.getMessage())

    r2 = mk(_logging.ERROR, "别的错误必须原样保留")
    check("其它 ERROR 通过且级别不变", flt.filter(r2) is True and r2.levelno == _logging.ERROR)

    r3 = mk(_logging.WARNING, "Failed to detach context")
    check("WARNING 级别不降级", flt.filter(r3) is True and r3.levelno == _logging.WARNING)


def main() -> int:
    import tempfile

    import pytest

    class _MP:
        """让 main() 直接跑也能用 —— 用 pytest 的 MonkeyPatch 独立实例。"""

        def __init__(self):
            self._mp = pytest.MonkeyPatch()

        def setattr(self, target, value, raising=True):
            return self._mp.setattr(target, value, raising=raising)

    def undo(self):
        self._mp.undo()

    print("=" * 70)
    print("追踪埋点测试")
    print("=" * 70)
    tests = [
        (test_build_without_keys_keeps_local_sink, 1),
        (test_unreachable_host_degrades_to_local, 1),
        (test_construct_timeout_gives_up, 0),
        (test_stages_and_ttft, 2),
        (test_cache_hit_has_no_generate, 2),
        (test_generate_failure_is_marked, 2),
        (test_sampled_out_still_local, 2),
        (test_langfuse_called_when_sampled, 2),
        (test_sink_failure_does_not_break_request, 2),
        (test_disabled_writes_nothing, 2),
        (test_replay_groups_by_trace, 2),
    ]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for fn, arity in tests:
            sub = root / fn.__name__
            sub.mkdir()
            mp = _MP()
            try:
                if arity == 0:
                    fn()
                elif arity == 1:
                    fn(sub)
                else:
                    fn(sub, mp)
            finally:
                mp.undo()
    print("=" * 70)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
