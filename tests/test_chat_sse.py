"""SSE 链路测试（端到端事件序列）。

为什么必须测事件**顺序**而不只是"能跑通"
---------------------------------------
前端和压测脚本都是按事件类型驱动的：
    meta      → 展示命中层级与版本号
    route     → 展示路由决策（这一条是演示的核心看点）
    tool_call → 展示"正在查工单"
    tool_result → 展示查到几条、来自 mock 还是 mcp
    docs      → 展示检索来源
    token     → 正文
    degraded  → 明确告诉用户这次质量降了
    done      → 收尾统计

这些事件的**相对顺序**就是前端的渲染逻辑。顺序错了，前端不会报错，
只会显示得不对 —— 这类问题在人工点几下的时候通常看不出来。
所以这里断言的是顺序，不是集合。

测试用假 cache / 假 rag，不打网络、不加载模型、不连向量库 ——
所以它能在 CI 里秒级跑完。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.chat import ChatRequest, chat_stream, route_preview  # noqa: E402
from app.cache.manager import CachedAnswer                        # noqa: E402
from app.core.config import settings                              # noqa: E402
from app.pipeline import rag                                      # noqa: E402
from app.tools import build_registry                              # noqa: E402

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
# 假的依赖
# =====================================================================
class FakeCache:
    """够用就好的假 Redis 缓存。"""

    def __init__(self, *, version: int = 1, hit: CachedAnswer | None = None):
        self.version = version
        self.hit = hit
        self.stored: list[CachedAnswer] = []
        self.lookups = 0
        self.singleflight = False

    async def current_kb_version(self) -> int:
        return self.version

    async def lookup(self, query, *, embedding=None):
        self.lookups += 1
        if self.hit is not None:
            return self.hit
        return CachedAnswer(answer="", queried=query, source="miss")

    async def acquire_singleflight(self, query) -> bool:
        self.singleflight = True
        return True

    async def release_singleflight(self, query) -> None:
        self.singleflight = False

    async def wait_for_result(self, query, *, embedding=None):
        return None

    async def store(self, query, value, *, embedding=None):
        self.stored.append(value)


class FakeRequest:
    def __init__(self, *, cache=None, tools=None):
        self.app = SimpleNamespace(state=SimpleNamespace(cache=cache, tools=tools))


def parse(raw_chunks: list[str]) -> list[dict]:
    """把 SSE 原始帧解析成事件列表。心跳帧（以 ':' 开头）被跳过。"""
    events: list[dict] = []
    for raw in raw_chunks:
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                continue
            events.append(json.loads(body))
    return events


async def collect(response) -> list[dict]:
    raw: list[str] = []
    async for chunk in response.body_iterator:
        raw.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return parse(raw)


async def ask(message: str, *, cache=None, tools=None, use_cache: bool = True) -> list[dict]:
    resp = await chat_stream(
        ChatRequest(message=message, use_cache=use_cache),
        FakeRequest(cache=cache, tools=tools),
        x_api_key=API_KEY,
    )
    return await collect(resp)


def types_of(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def install_fakes(monkeypatch, *, chunks=None, tool_context_seen: list | None = None,
                  generate_raises: bool = False, recall_raises: bool = False):
    """替换掉所有需要网络/模型的依赖。"""
    chunks = chunks if chunks is not None else [
        rag.Chunk(doc_id="rb-mysql-conn-pool", chunk_id="rb-mysql-conn-pool#01",
                  text="连接池耗尽处置：先查 processlist", source="rb-mysql-conn-pool", score=0.9),
        rag.Chunk(doc_id="alert-p1-db-conn", chunk_id="alert-p1-db-conn#01",
                  text="连接数超过 max_connections 85% 告警", source="alert-p1-db-conn",
                  score=0.8, recall_source="bm25"),
    ]

    async def fake_embed_one(text):
        return [0.1] * 8

    async def fake_recall_detailed(query, *, embedding=None, top_k=None, fusion=None):
        if recall_raises:
            raise RuntimeError("向量库连接失败")
        return rag.RecallOutcome(
            chunks=list(chunks), channels={"vector": 2, "bm25": 1}, fused=True,
        )

    async def fake_rerank(query, items, top_k=None, **kwargs):
        return list(items)[: (top_k or 5)]

    async def fake_generate(question, items, *, tool_context="", history=""):
        if tool_context_seen is not None:
            tool_context_seen.append(tool_context)
        if generate_raises:
            raise RuntimeError("LLM 连接超时")
        for piece in ("根据", "资料", "回答完毕"):
            yield piece

    monkeypatch.setattr("app.pipeline.orchestrator.embed_one", fake_embed_one)
    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall_detailed)
    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", fake_rerank)
    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)


# =====================================================================
# 用例
# =====================================================================
def test_chitchat(monkeypatch) -> None:
    print("chitchat：不检索、不调工具、不走 LLM")
    called = {"recall": 0, "generate": 0}

    async def fake_embed_one(text):
        raise AssertionError("闲聊不该向量化")

    async def fake_recall(**kwargs):
        called["recall"] += 1
        raise AssertionError("闲聊不该检索")

    async def fake_generate(*a, **k):
        called["generate"] += 1
        yield ""

    monkeypatch.setattr("app.pipeline.orchestrator.embed_one", fake_embed_one)
    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall)
    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)

    cache = FakeCache()
    events = asyncio.run(ask("你好，你是谁？你能帮我做什么？", cache=cache))

    check("不检索", called["recall"] == 0, str(called))
    check("不走 LLM", called["generate"] == 0, str(called))
    check("不查缓存", cache.lookups == 0, f"lookups={cache.lookups}")
    check("meta 标为 bypass", events[0].get("cache") == "bypass", str(events[0]))
    check("meta 的 route 是 chitchat", events[0].get("route") == "chitchat", str(events[0]))
    check("含 route 事件", types_of(events)[1] == "route", str(types_of(events)))
    check("有 token", any(e["type"] == "token" for e in events), str(types_of(events)))
    check("done 标为 bypass", events[-1]["type"] == "done" and events[-1]["cache"] == "bypass",
          str(events[-1]))
    check("闲聊答案不写缓存", cache.stored == [], str(len(cache.stored)))


def test_rag_flow(monkeypatch) -> None:
    print("rag：检索 → 文档 → token → done")
    install_fakes(monkeypatch)
    # 用"连接池耗尽怎么处理"这类知识题，避免命中实时信号
    events = asyncio.run(ask("连接池耗尽该怎么处理？", cache=FakeCache()))
    kinds = types_of(events)

    check("事件顺序 meta→route→docs→token→done",
          kinds[0] == "meta" and kinds[1] == "route" and "docs" in kinds
          and kinds[-1] == "done", str(kinds))
    check("docs 在 token 之前", kinds.index("docs") < kinds.index("token"), str(kinds))
    check("route 意图为 rag", next(e for e in events if e["type"] == "route")["intent"] == "rag",
          str(next(e for e in events if e["type"] == "route")))
    check("不产生工具事件", "tool_call" not in kinds and "tool_result" not in kinds, str(kinds))

    docs = next(e for e in events if e["type"] == "docs")
    check("docs 带来源", docs["sources"] == ["rb-mysql-conn-pool", "alert-p1-db-conn"],
          str(docs["sources"]))
    check("docs 带每路命中数", docs.get("channels") == {"vector": 2, "bm25": 1}, str(docs.get("channels")))
    check("docs 带融合标记", docs.get("fused") is True, str(docs.get("fused")))
    check("docs 带每块召回来源", docs.get("recall_sources") == ["vector", "bm25"],
          str(docs.get("recall_sources")))

    done = events[-1]
    check("done 含分阶段耗时", "stage_ms" in done and "recall" in done["stage_ms"], str(done))
    check("done 的 route 为 rag", done.get("route") == "rag", str(done))


def test_tool_flow(monkeypatch) -> None:
    print("tool：工具调用事件 + 结果进生成上下文")
    seen: list[str] = []
    install_fakes(monkeypatch, tool_context_seen=seen)
    registry = build_registry(mock_path="eval/mock_records.json")

    events = asyncio.run(ask(
        "帮我查一下工单 INC-2026-0412 的处理进度。", cache=FakeCache(), tools=registry
    ))
    kinds = types_of(events)

    check("含 tool_call", "tool_call" in kinds, str(kinds))
    check("含 tool_result", "tool_result" in kinds, str(kinds))
    check("tool_call 在 tool_result 之前", kinds.index("tool_call") < kinds.index("tool_result"),
          str(kinds))
    check("tool_result 在 token 之前", kinds.index("tool_result") < kinds.index("token"), str(kinds))
    check("tool 意图不检索（无 docs）", "docs" not in kinds, str(kinds))

    call = next(e for e in events if e["type"] == "tool_call")
    check("调用的是按单号查询", call["name"] == "query_ticket_by_no", str(call))
    check("参数带工单号", call["args"] == {"ticket_no": "INC-2026-0412"}, str(call["args"]))

    res = next(e for e in events if e["type"] == "tool_result")
    check("工具成功且标注来源", res["ok"] is True and res["source"] == "mock", str(res))
    check("结果条数为 1", res["records"] == 1, str(res))

    # 工具结果必须真的进了生成上下文 —— 否则"调了工具但答案没用到"，
    # 这是一种看起来完全正常的失败。
    check("工具结果进入生成上下文", bool(seen) and "INC-2026-0412" in seen[0], str(seen[:1])[:200])
    check("上下文标注了来源", "来源 mock" in seen[0], seen[0][:120])


def test_hybrid_flow(monkeypatch) -> None:
    print("hybrid：工具与检索两路都要，且事件齐全")
    seen: list[str] = []
    install_fakes(monkeypatch, tool_context_seen=seen)
    registry = build_registry(mock_path="eval/mock_records.json")

    events = asyncio.run(ask(
        "连接数超过多少会告警？另外把数据库连接相关的历史工单也给我看一下。",
        cache=FakeCache(), tools=registry,
    ))
    kinds = types_of(events)
    route = next(e for e in events if e["type"] == "route")

    check("路由为 hybrid", route["intent"] == "hybrid", str(route))
    check("needs_rag 与 needs_tool 都为真", route["needs_rag"] and route["needs_tool"], str(route))
    check("含工具事件", "tool_call" in kinds and "tool_result" in kinds, str(kinds))
    check("含 docs 事件", "docs" in kinds, str(kinds))
    check("工具结果进了上下文", bool(seen) and "工单" in seen[0], str(seen[:1])[:160])
    check("同时带知识库资料", bool(seen) and "连接池" in seen[0], seen[0][:200])


def test_cache_hit(monkeypatch) -> None:
    print("缓存命中：直接推流，不检索不调工具")
    install_fakes(monkeypatch)
    called = {"recall": 0}

    async def fake_recall(**kwargs):
        called["recall"] += 1
        raise AssertionError("缓存命中不该检索")

    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall)

    cached = CachedAnswer(answer="这是缓存里的答案", queried="x", source="semantic",
                          similarity=0.981, route="rag", tokens_saved=42)
    cache = FakeCache(hit=cached)
    events = asyncio.run(ask("连接池耗尽该怎么处理？", cache=cache))

    check("未触发检索", called["recall"] == 0, str(called))
    check("meta 标为 semantic", events[0]["cache"] == "semantic", str(events[0]))
    check("meta 带相似度", events[0]["similarity"] == 0.981, str(events[0]))
    check("仍推 route 事件（可回放决策）", events[1]["type"] == "route", str(types_of(events)))
    check("仅 token + done", set(types_of(events)) == {"meta", "route", "token", "done"},
          str(types_of(events)))
    check("done 带 tokens_saved", events[-1]["tokens_saved"] == 42, str(events[-1]))


def test_runtime_kb_version(monkeypatch) -> None:
    print("运行时版本号：取 cache 的当前值，不是 settings 快照")
    install_fakes(monkeypatch)
    # settings.kb_version 是启动时的快照；打一次 /admin/kb/bump 之后它就过期了。
    # 事件流里报旧版本号会让用户以为 bump 没生效 —— 而其实生效了。
    cache = FakeCache(version=7)
    events = asyncio.run(ask("连接池耗尽该怎么处理？", cache=cache))
    meta = events[0]
    check("meta 用的是 cache 的版本号", meta["kb_version"] == 7, str(meta))
    check("与 settings 快照不同（证明真的读了运行时值）",
          meta["kb_version"] != settings.kb_version, f"{meta['kb_version']} vs {settings.kb_version}")


def test_degradation(monkeypatch) -> None:
    print("降级：依赖挂掉不返回 500，而是可见地降级")
    # ① 召回全挂
    install_fakes(monkeypatch, recall_raises=True)
    events = asyncio.run(ask("连接池耗尽该怎么处理？", cache=FakeCache()))
    kinds = types_of(events)
    check("召回失败 → degraded 事件", "degraded" in kinds, str(kinds))
    deg = next(e for e in events if e["type"] == "degraded")
    check("degraded 标明 stage", deg["stage"] == "recall", str(deg))
    check("召回失败仍继续生成", "token" in kinds and kinds[-1] == "done", str(kinds))

    # ② 生成挂
    install_fakes(monkeypatch, generate_raises=True)
    events = asyncio.run(ask("连接池耗尽该怎么处理？", cache=FakeCache()))
    kinds = types_of(events)
    check("生成失败 → degraded + error", "degraded" in kinds and "error" in kinds, str(kinds))
    check("生成失败仍有 done（流要正常收尾）", kinds[-1] == "done", str(kinds))

    # ③ 工具层不可用（tool 意图）
    install_fakes(monkeypatch)
    events = asyncio.run(ask("帮我查一下工单 INC-2026-0412 的处理进度。", cache=FakeCache(), tools=None))
    degs = [e for e in events if e["type"] == "degraded"]
    check("工具层缺失 → degraded 说明", any(d["stage"] == "tools" for d in degs), str(degs))
    check("工具缺失时仍给出回答（不 500）", types_of(events)[-1] == "done", str(types_of(events)))


def test_rerank_switch(monkeypatch) -> None:
    """`RERANK_ENABLED=false` 必须真的不调重排，且**如实标注**。

    为什么这个开关值得一条用例：重排在纯 CPU 上实测是**几十秒每题的
    量级**（见 scripts/bench_rerank.py），所以压测和演示都必然要关掉它。
    而"关掉"有个很容易犯的错 —— 静默跳过。
    那样 SSE 里就看不出这次答案是走了一条更弱的链路，
    于是演示的人、看指标的人都会以为看到的是完整链路。
    """
    print("重排开关：关掉后不调用，且如实标注")
    install_fakes(monkeypatch)
    calls: list[str] = []

    async def spy_rerank(query, items, top_k=None, **kwargs):
        calls.append(query)
        return list(items)

    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", spy_rerank)
    monkeypatch.setattr(settings, "rerank_enabled", False)

    events = asyncio.run(ask("连接池耗尽该怎么处理？", cache=FakeCache()))
    kinds = types_of(events)
    degs = [e for e in events if e["type"] == "degraded"]

    check("关掉后不再调用重排", calls == [], str(calls))
    check("如实标注 disabled（不静默跳过）",
          any("rerank:disabled" in str(d.get("reason", "")) for d in degs), str(degs))
    check("关掉后仍正常回答并收尾", "token" in kinds and kinds[-1] == "done", str(kinds))
    docs = next(e for e in events if e["type"] == "docs")
    check("仍然给出文档来源（上下文没丢）", bool(docs.get("sources")), str(docs))
    check("关掉重排后每篇文档仍受块数上限约束",
          len(docs.get("sources") or []) <= settings.top_k_rerank, str(docs.get("sources")))


def test_rerank_pool_is_recall_pool(monkeypatch) -> None:
    """送进重排的候选池必须是**召回池的全量**，不能是某个写死的数字。

    这条用例来自一次真实的坑：`scripts/bench_rerank.py` 最初按
    "每次重排 20 对"报单题耗时，而线上默认配置
    （`top_k_retrieve=20` + RRF + 每篇文档上限 3）融合去重后，
    实测候选池是**中位 29 块**（min 21 / max 36，见 `--from-recall`）。
    也就是说那个成本数字**低估了约 1.45 倍**。

    危险之处在于：这个错误**不会让任何测试变红**，
    也不会报错 —— 因为"20"这个数字从来没进过代码，
    它只是活在 benchmark 脚本的参数默认值里。
    所以这里把它钉住：谁把重排前的候选池截断成一个常数，这条用例就红。
    """
    print("重排候选池 = 召回池全量（不是写死的常数）")
    # 刻意用 7：既不是 top_k_retrieve(20) 也不是 top_k_rerank(5)，
    # 这样"恰好等于某个线上默认值"不会让用例假通过。
    pool = [
        rag.Chunk(doc_id=f"doc-{i}", chunk_id=f"doc-{i}#01", text=f"第 {i} 块",
                  source=f"doc-{i}", score=1.0 - i / 100)
        for i in range(7)
    ]
    install_fakes(monkeypatch, chunks=pool)
    seen: list[int] = []

    async def spy_rerank(query, items, top_k=None, **kwargs):
        seen.append(len(items))
        return list(items)[: (top_k or 5)]

    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", spy_rerank)

    asyncio.run(ask("连接池耗尽该怎么处理？", cache=FakeCache()))

    check("重排确实被调用了一次", len(seen) == 1, str(seen))
    check(f"送进重排的是召回池全量 {len(pool)} 块（未被截断成常数）",
          seen == [len(pool)], str(seen))


def test_no_plan_visible(monkeypatch) -> None:
    """抽不到实体却走了实时路，必须让用户看见 —— 否则用户只看到"没查到"，
    却不知道系统压根没去查。

    这里直接注入空计划，而不是找一个"恰好抽不出实体"的句子：
    靠措辞凑用例，测的是那句话而不是那段逻辑，换个句子就失效了。
    """
    print("工具计划为空时给出可见说明")
    install_fakes(monkeypatch)
    monkeypatch.setattr("app.pipeline.orchestrator.plan", lambda decision, question: [])
    registry = build_registry(mock_path="eval/mock_records.json")

    events = asyncio.run(ask(
        "帮我查一下工单 INC-2026-0412 的处理进度。", cache=FakeCache(), tools=registry
    ))
    kinds = types_of(events)
    degs = [e for e in events if e["type"] == "degraded"]

    check("计划为空 → degraded(no_plan)", any(d.get("reason") == "no_plan" for d in degs), str(degs))
    check("计划为空时不发 tool_call", "tool_call" not in kinds, str(kinds))
    check("仍然正常收尾", kinds[-1] == "done", str(kinds))


def test_route_preview_endpoint() -> None:
    print("/chat/route 只做路由，不执行任何动作")
    resp = asyncio.run(route_preview(
        ChatRequest(message="资产编码 SRV-0088 现在什么状态？"), x_api_key=API_KEY
    ))
    check("返回 intent", resp["intent"] == "tool", str(resp.get("intent")))
    check("返回工具计划", resp["tool_plan"][0]["name"] == "get_asset_by_code", str(resp["tool_plan"]))
    check("返回决策依据", bool(resp["reason"]) and bool(resp["signals"]), str(resp))
    check("返回实体", resp["entities"]["asset_codes"] == ["SRV-0088"], str(resp["entities"]))


def test_api_key_required() -> None:
    print("鉴权")
    try:
        asyncio.run(chat_stream(ChatRequest(message="hi"), FakeRequest(), x_api_key=None))
        check("无 key 应被拒绝", False, "竟然通过了")
    except Exception as exc:  # HTTPException
        check("无 key 被拒绝", getattr(exc, "status_code", None) == 401, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("=" * 70)
    print("SSE 链路测试")
    print("=" * 70)
    import pytest

    class _MP:
        """让 main() 直接跑也能用 —— 用 pytest 的 MonkeyPatch 独立实例。"""

        def __init__(self):
            self._mp = pytest.MonkeyPatch()

        def setattr(self, target, value, raising=True):
            return self._mp.setattr(target, value, raising=raising)

    tests = [
        test_chitchat, test_rag_flow, test_tool_flow, test_hybrid_flow,
        test_cache_hit, test_runtime_kb_version, test_degradation,
        test_no_plan_visible,
    ]
    for fn in tests:
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp._mp.undo()
    test_route_preview_endpoint()
    test_api_key_required()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
