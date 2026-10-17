"""LangGraph 编排的结构守护。

为什么图本身值得测试（而不只测端点行为）
----------------------------------------
端点级用例（test_chat_sse）验证的是"事件流对不对"；
但如果有人把扇出改回串行、把条件边接错节点，
只要事件**碰巧**还按老顺序出来，端点用例照样全绿 ——
比如 hybrid 的 tools ∥ recall 并行被改成先 tools 后 recall，
事件顺序不变，只是延迟悄悄变成两路相加。
所以这里直接钉住**拓扑本身**：图长什么样，是契约的一部分。

另一个被钉住的关键点：图必须**编译一次、复用多次**。
如果 get_graph() 每个请求都重新 build+compile，
LangGraph 的图编译开销会平摊进每一次 TTFT。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline import intent as intent_mod                      # noqa: E402
from app.pipeline.orchestrator import (                            # noqa: E402
    _after_cache_lookup,
    _after_route,
    _after_singleflight,
    get_graph,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


EXPECTED_NODES = {
    "route", "chitchat", "memory_load", "embed", "cache_lookup",
    "singleflight", "dispatch", "tools", "recall", "generate",
}


def test_graph_topology() -> None:
    print("图拓扑：节点齐全，编译一次复用多次")
    g = get_graph()
    nodes = set(g.get_graph().nodes)
    check("全部 10 个节点在图里", EXPECTED_NODES <= nodes, str(sorted(nodes - EXPECTED_NODES)))
    check("没有多余节点", nodes - {"__start__", "__end__"} == EXPECTED_NODES,
          str(nodes - {"__start__", "__end__"} - EXPECTED_NODES))
    check("get_graph() 复用同一实例（编译只付一次）", get_graph() is g, "每次调用都重建了图")


def test_route_conditional() -> None:
    print("路由分支：chitchat 短路，其余进主链路")
    state_chit = {"decision": intent_mod.decide("你好，你是谁")}
    check("闲聊 → chitchat 节点", _after_route(state_chit) == "chitchat",
          str(_after_route(state_chit)))
    for msg, name in [
        ("连接池耗尽该怎么处理？", "rag"),
        ("查一下工单 INC-2026-0412 的进度", "tool"),
        ("连接数告警阈值是多少？顺便查下相关工单", "hybrid"),
    ]:
        state = {"decision": intent_mod.decide(msg)}
        got = _after_route(state)
        check(f"{name} → 主链路（memory_load）", got == "memory_load", got)


def test_cache_and_singleflight_conditionals() -> None:
    print("缓存与单飞的分支：命中/跟随者终局，未命中/领跑者继续")
    check("缓存未命中 → singleflight", _after_cache_lookup({"hit": None}) == "singleflight",
          "")
    check("缓存命中 → END", _after_cache_lookup({"hit": object()}) == "__end__", "")
    check("单飞领跑者 → dispatch", _after_singleflight({"waited": None}) == "dispatch", "")
    check("单飞跟随者拿到结果 → END",
          _after_singleflight({"waited": object()}) == "__end__", "")


def test_fanout_is_parallel() -> None:
    """hybrid 的 tools 与 recall 必须是**并行**边，不能串行。

    这是从旧实现继承下来的设计约束：两路无依赖，顺序执行等于把耗时相加。
    判据：dispatch 的出边同时指向 tools 和 recall（扇出），
    而 tools 和 recall 的出边都指向 generate（汇合）。
    """
    print("扇出：dispatch → {tools, recall}，两路汇合于 generate")
    g = get_graph()
    edges = [(e.source, e.target) for e in g.get_graph().edges]
    outs = {}
    for src, dst in edges:
        outs.setdefault(src, set()).add(dst)
    check("dispatch 扇出到 tools 与 recall",
          {"tools", "recall"} <= outs.get("dispatch", set()), str(outs.get("dispatch")))
    check("tools 与 recall 都汇合到 generate",
          "generate" in outs.get("tools", set()) and "generate" in outs.get("recall", set()),
          f"tools→{outs.get('tools')} recall→{outs.get('recall')}")
    check("generate 后没有再进别的节点（终局）", outs.get("generate") == {"__end__"},
          str(outs.get("generate")))


def main() -> int:
    print("=" * 70)
    print("LangGraph 编排结构测试")
    print("=" * 70)
    test_graph_topology()
    test_route_conditional()
    test_cache_and_singleflight_conditionals()
    test_fanout_is_parallel()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
