"""三层记忆测试：事实抽取 / 会话存储 / 端到端注入 / 降级可见。

为什么记忆必须有一整套自己的测试
--------------------------------
记忆的特殊失败模式是"静默失效"：Redis 挂了、键写错了、
TTL 配成 0 了 —— 问答请求**照常返回**，只是不带历史。
用户不会投诉"回答缺了上下文"，因为根本不知道有这回事。
所以这里的重点是三类断言：
  1. 注入真的发生（第二轮的 prompt 里有第一轮的内容）；
  2. 降级真的可见（记忆挂了 → SSE 里出现 degraded stage=memory）；
  3. 边界行为正确（滑窗截断、事实去重、非法 session_id）。

端到端用同一进程内的 InMemorySessionStore（与 Redis 版同接口）：
测的是编排层"什么时候 load / 什么时候 save / 失败怎么办"，
存储实现本身的差异不是这里的对象。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.chat import ChatRequest, chat_stream               # noqa: E402
from app.core.config import settings                            # noqa: E402
from app.memory.facts import extract_facts                      # noqa: E402
from app.memory.session import (                                # noqa: E402
    InMemorySessionStore,
    RedisSessionStore,
    safe_sid,
    render_memory_context,
)
from app.pipeline import rag                                    # noqa: E402

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
# 测试基建
# =====================================================================
class FakeRequest:
    def __init__(self, *, cache=None, tools=None, memory=None):
        self.app = SimpleNamespace(
            state=SimpleNamespace(cache=cache, tools=tools, memory=memory)
        )


class FakeCache:
    async def current_kb_version(self) -> int:
        return 1

    async def lookup(self, query, *, embedding=None):
        from app.cache.manager import CachedAnswer

        return CachedAnswer(answer="", queried=query, source="miss")

    async def get_semantic(self, query, embedding):
        return None

    async def acquire_singleflight(self, query) -> bool:
        return True

    async def release_singleflight(self, query) -> None:
        return None

    async def wait_for_result(self, query, *, embedding=None):
        return None

    async def store(self, query, value, *, embedding=None):
        return None


def parse(raw_chunks: list[str]) -> list[dict]:
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


def install_generate_spy(monkeypatch, history_seen: list[str]):
    """替换生成，并记录每次收到的 history 注入文本。"""

    async def fake_embed_one(text):
        return [0.1] * 8

    async def fake_recall_detailed(query, *, embedding=None, **kwargs):
        return rag.RecallOutcome(
            chunks=[rag.Chunk(doc_id="d1", chunk_id="d1#01", text="t",
                              source="d1", score=0.9)],
            channels={"bm25": 1}, fused=False,
        )

    async def fake_rerank(query, items, top_k=None, **kwargs):
        return list(items)

    async def fake_generate(question, items, *, tool_context="", history=""):
        history_seen.append(history)
        for piece in ("答", "案"):
            yield piece

    monkeypatch.setattr("app.pipeline.orchestrator.embed_one", fake_embed_one)
    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall_detailed)
    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", fake_rerank)
    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)


async def ask(message: str, *, memory=None, session_id=None) -> list[dict]:
    resp = await chat_stream(
        ChatRequest(message=message, session_id=session_id),
        FakeRequest(cache=FakeCache(), memory=memory),
        x_api_key=API_KEY,
    )
    return await collect(resp)


# =====================================================================
# ① 事实抽取
# =====================================================================
def test_facts_extraction() -> None:
    print("长期事实抽取：只抽稳定属性，否定护栏，条数封顶")
    got = extract_facts("我们生产用的是 MySQL 8.0，我是数据库工程师")
    check("抽到技术栈", any("MySQL 8.0" in f for f in got), str(got))
    check("抽到角色", any("工程师" in f for f in got), str(got))

    got = extract_facts("我们不用 MySQL，我们用 PostgreSQL")
    check("否定片段不进事实库", all("不用 MySQL" not in f for f in got), str(got))

    got = extract_facts("现在连接池满了")
    check("瞬时状态不进事实库", got == [], str(got))

    got = extract_facts("我们生产用的是 A，我们生产用的是 A，我们生产用的是 B")
    check("同一条消息内去重", len(got) == len(set(got)) and len(got) <= 3, str(got))

    check("空输入安全", extract_facts("") == [], "")
    check("None 安全", extract_facts(None) == [], "")


def testsafe_sid() -> None:
    print("session_id 白名单：键注入从源头堵死")
    check("正常 id 通过", safe_sid("user-001") == "user-001", "")
    check("空 / None 按'无会话'处理", safe_sid(None) == "" and safe_sid("") == "", "")
    for bad in ["a: b", "键*注入", "x" * 65, "a\nb"]:
        check(f"非法 id 被拒：{bad[:8]!r}", safe_sid(bad) == "", safe_sid(bad))


# =====================================================================
# ② 会话存储（InMemory 与 Redis 降级行为）
# =====================================================================
def test_inmemory_store() -> None:
    print("InMemory 存储：滑窗截断 / 事实封顶 / 接口对齐")
    store = InMemorySessionStore()

    async def run():
        for i in range(3):
            err = await store.save_turn(
                "u1", question=f"问题{i}", answer=f"答案{i}", route="rag"
            )
            assert err is None
        return await store.load("u1")

    ctx = asyncio.run(run())
    check("保存与读回一致", [t["q"] for t in ctx["history"]] == ["问题0", "问题1", "问题2"],
          str(ctx["history"]))

    # 滑窗：把上限临时调成 2，下一轮保存后只剩最近 2 轮
    old = settings.session_max_turns
    settings.session_max_turns = 2
    try:
        asyncio.run(store.save_turn("u1", question="问题3", answer="答案3", route="rag"))
        ctx = asyncio.run(store.load("u1"))
        check("滑窗截断到最近 N 轮",
              [t["q"] for t in ctx["history"]] == ["问题2", "问题3"], str(ctx["history"]))
    finally:
        settings.session_max_turns = old

    # 事实封顶
    old_max = settings.facts_max
    settings.facts_max = 2
    try:
        asyncio.run(store.save_turn("u1", question="q", answer="a", route="rag",
                                    facts=["事实A", "事实B", "事实C"]))
        ctx = asyncio.run(store.load("u1"))
        check("事实上限生效（留最新的）", ctx["facts"] == ["事实B", "事实C"], str(ctx["facts"]))
    finally:
        settings.facts_max = old_max

    check("非法 session_id 不写入不报错",
          asyncio.run(store.save_turn("a: b", question="q", answer="a", route="rag")) is None
          and asyncio.run(store.load("a: b")) == {"history": [], "facts": []}, "")


def test_render_memory_context() -> None:
    print("记忆渲染：历史 + 事实 → 可注入文本")
    text = render_memory_context({
        "history": [{"q": "连接池耗尽怎么办", "a": "先查 processlist", "route": "rag"}],
        "facts": ["我们生产用的是 MySQL 8.0"],
    })
    check("历史进渲染", "连接池耗尽怎么办" in text and "先查 processlist" in text, text)
    check("事实进渲染", "MySQL 8.0" in text, text)
    check("空上下文渲染为空串", render_memory_context({"history": [], "facts": []}) == "", "")


def test_redis_store_degradation() -> None:
    """Redis 挂掉：load 返回空上下文、save 返回错误串 —— 绝不抛异常。"""
    print("Redis 存储降级：吞异常，错误可见")

    class DeadRedis:
        def lrange(self, *a, **k):
            raise ConnectionError("down")

        def hgetall(self, *a, **k):
            raise ConnectionError("down")

        def pipeline(self, *a, **k):
            raise ConnectionError("down")

    store = RedisSessionStore(DeadRedis())
    ctx = asyncio.run(store.load("u1"))
    check("load 失败返回空上下文", ctx == {"history": [], "facts": []}, str(ctx))
    check("load 失败标记 last_error", store.last_error == "ConnectionError",
          str(store.last_error))
    err = asyncio.run(store.save_turn("u1", question="q", answer="a", route="rag"))
    check("save 失败返回 memory:<Err>", err == "memory:ConnectionError", str(err))


# =====================================================================
# ③ 端到端：注入、可见性、边界
# =====================================================================
def test_session_flow_e2e(monkeypatch) -> None:
    """两轮对话：第一轮说的背景，第二轮真的进了 prompt。"""
    print("端到端：第二轮生成收到第一轮历史与事实")
    history_seen: list[str] = []
    install_generate_spy(monkeypatch, history_seen)
    store = InMemorySessionStore()

    events1 = asyncio.run(ask(
        "我们生产用的是 MySQL 8.0。连接池耗尽该怎么处理？",
        memory=store, session_id="u-e2e",
    ))
    check("第一轮正常收尾", events1[-1]["type"] == "done", str(events1[-1]))
    check("第一轮无历史可注入", history_seen and history_seen[0] == "",
          str(history_seen))

    events2 = asyncio.run(ask("那这个版本有什么已知的坑？",
                              memory=store, session_id="u-e2e"))
    check("第二轮正常收尾", events2[-1]["type"] == "done", str(events2[-1]))
    injected = history_seen[1] if len(history_seen) > 1 else ""
    check("第二轮注入了第一轮的问题", "连接池耗尽" in injected, injected[:120])
    check("第二轮注入了第一轮的回答", "答案" in injected, injected[:120])
    check("第二轮注入了抽出的长期事实", "MySQL 8.0" in injected, injected[:160])

    meta2 = events2[0]
    check("meta 带记忆统计", meta2.get("memory", {}).get("history_turns") == 1
          and meta2.get("memory", {}).get("facts") == 1, str(meta2))
    check("meta 仍是第一个事件", meta2["type"] == "meta", str(meta2["type"]))

    # 换会话：记忆不串 —— 无状态横向扩展的行为前提
    asyncio.run(ask("别的会话的问题", memory=store, session_id="u-other"))
    third = history_seen[2] if len(history_seen) > 2 else ""
    check("不同 session_id 之间记忆隔离", "连接池耗尽" not in third, third[:120])


def test_memory_degradation_visible(monkeypatch) -> None:
    """记忆挂了：请求照常完成，但 SSE 里必须出现 stage=memory 的降级。"""
    print("记忆降级：可见，不 500，不静默")

    class BrokenStore(InMemorySessionStore):
        async def load(self, session_id):
            raise ConnectionError("redis down")

    history_seen: list[str] = []
    install_generate_spy(monkeypatch, history_seen)

    events = asyncio.run(ask("连接池耗尽该怎么处理？",
                             memory=BrokenStore(), session_id="u-broken"))
    kinds = [e.get("type") for e in events]
    degs = [e for e in events if e["type"] == "degraded"]
    check("出现 stage=memory 的 degraded 事件",
          any(d.get("stage") == "memory" for d in degs), str(degs))
    check("降级事件在 meta 之后（meta 仍是第一帧）",
          kinds[0] == "meta" and kinds.index("degraded") > 0, str(kinds))
    check("请求照常完成", kinds[-1] == "done", str(kinds))
    check("生成未带历史注入", history_seen and history_seen[0] == "", str(history_seen))


def test_save_failure_visible(monkeypatch) -> None:
    """保存失败（写历史/事实时 Redis 断了）也要可见，且不能影响收尾帧。"""
    print("记忆保存失败：degraded 在 done 之前，流正常收尾")

    class SaveBrokenStore(InMemorySessionStore):
        async def save_turn(self, session_id, **kwargs):
            return "memory:ConnectionError"

    history_seen: list[str] = []
    install_generate_spy(monkeypatch, history_seen)

    events = asyncio.run(ask("连接池耗尽该怎么处理？",
                             memory=SaveBrokenStore(), session_id="u-save"))
    kinds = [e.get("type") for e in events]
    degs = [e for e in events if e["type"] == "degraded"]
    check("保存失败产生 degraded", any(d.get("stage") == "memory" for d in degs), str(degs))
    check("done 仍是最后一帧", kinds[-1] == "done", str(kinds))


def test_no_session_no_memory(monkeypatch) -> None:
    """不带 session_id：记忆层一次都不该被碰。"""
    print("无 session_id：记忆层零调用")

    class SpyStore(InMemorySessionStore):
        def __init__(self):
            super().__init__()
            self.load_calls = 0
            self.save_calls = 0

        async def load(self, session_id):
            self.load_calls += 1
            return await super().load(session_id)

        async def save_turn(self, session_id, **kwargs):
            self.save_calls += 1
            return await super().save_turn(session_id, **kwargs)

    history_seen: list[str] = []
    install_generate_spy(monkeypatch, history_seen)
    store = SpyStore()

    events = asyncio.run(ask("连接池耗尽该怎么处理？", memory=store, session_id=None))
    check("不触发 load", store.load_calls == 0, str(store.load_calls))
    check("不触发 save", store.save_calls == 0, str(store.save_calls))
    check("meta 不带 memory 字段", "memory" not in events[0], str(events[0]))
    check("正常收尾", events[-1]["type"] == "done", str(events[-1]))


def test_chitchat_records_turn(monkeypatch) -> None:
    """闲聊也记会话：连续对话里"详细说说第 2 点"需要知道上一轮是什么。"""
    print("闲聊也记会话历史")

    async def fake_generate(*a, **k):  # 闲聊不该走到这里
        raise AssertionError("闲聊不该走 LLM")

    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)
    store = InMemorySessionStore()

    events = asyncio.run(ask("你好，你是谁？", memory=store, session_id="u-chat"))
    check("闲聊正常收尾", events[-1]["type"] == "done", str(events[-1]))
    ctx = asyncio.run(store.load("u-chat"))
    check("闲聊轮被记录", len(ctx["history"]) == 1 and ctx["history"][0]["route"] == "chitchat",
          str(ctx["history"]))


def main() -> int:
    print("=" * 70)
    print("三层记忆测试")
    print("=" * 70)
    test_facts_extraction()
    testsafe_sid()
    test_inmemory_store()
    test_render_memory_context()
    test_redis_store_degradation()
    test_session_flow_e2e(monkeypatch := _make_mp())
    test_memory_degradation_visible(monkeypatch)
    test_save_failure_visible(monkeypatch)
    test_no_session_no_memory(monkeypatch)
    test_chitchat_records_turn(monkeypatch)
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


def _make_mp():
    import pytest

    return pytest.MonkeyPatch()


if __name__ == "__main__":
    raise SystemExit(main())
