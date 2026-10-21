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
    _after_cache_exact,
    _after_cache_semantic,
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
    "route", "chitchat", "memory_load",
    # D4 性能重构：缓存拆成 exact / semantic 两段，embed 只由未命中的请求支付
    # （exact 命中不再付 BGE 编码 —— 旧图 stage p50 embed_query 6.33s 的瓶颈）。
    "cache_exact", "embed", "cache_semantic",
    "singleflight", "dispatch", "tools", "recall", "generate",
    # D3：汇合点上移 + 挂起待确认终局（见 orchestrator.node_tools_done 的说明）
    "tools_done", "await_confirm",
}


def test_graph_topology() -> None:
    print("图拓扑：节点齐全，编译一次复用多次")
    g = get_graph()
    nodes = set(g.get_graph().nodes)
    check(f"全部 {len(EXPECTED_NODES)} 个节点在图里", EXPECTED_NODES <= nodes,
          str(sorted(nodes - EXPECTED_NODES)))
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
    check("exact 未命中 → embed（先编码再查语义）",
          _after_cache_exact({"hit": None}) == "embed",
          str(_after_cache_exact({"hit": None})))
    check("exact 命中 → END（不付编码费）",
          _after_cache_exact({"hit": object()}) == "__end__", "")
    check("语义未命中 → singleflight",
          _after_cache_semantic({"hit": None}) == "singleflight", "")
    check("语义命中 → END",
          _after_cache_semantic({"hit": object()}) == "__end__", "")
    check("单飞领跑者 → dispatch", _after_singleflight({"waited": None}) == "dispatch", "")
    check("单飞跟随者拿到结果 → END",
          _after_singleflight({"waited": object()}) == "__end__", "")


def test_fanout_is_parallel() -> None:
    """hybrid 的 tools 与 recall 必须是**并行**边，不能串行。

    这是从旧实现继承下来的设计约束：两路无依赖，顺序执行等于把耗时相加。
    判据：dispatch 的出边同时指向 tools 和 recall（扇出），
    而 tools 和 recall 的出边都指向同一个汇合点（汇合）。

    D3 把汇合点从 generate 前移到了 tools_done：
    挂起待确认必须在**两路都跑完**之后才决定去向。如果条件边直接挂在 tools 上，
    recall→generate 那条边仍会触发 generate —— 于是"等人确认"的同时
    模型已经生成了一段"已为你重启"的答案。
    """
    print("扇出：dispatch → {tools, recall}，两路汇合于 tools_done")
    g = get_graph()
    edges = [(e.source, e.target) for e in g.get_graph().edges]
    outs = {}
    for src, dst in edges:
        outs.setdefault(src, set()).add(dst)
    check("dispatch 扇出到 tools 与 recall",
          {"tools", "recall"} <= outs.get("dispatch", set()), str(outs.get("dispatch")))
    check("tools 与 recall 都汇合到 tools_done",
          outs.get("tools") == {"tools_done"} and outs.get("recall") == {"tools_done"},
          f"tools→{outs.get('tools')} recall→{outs.get('recall')}")
    check("tools_done 是唯一通向 generate / await_confirm 的入口",
          "tools_done" not in outs.get("generate", set())
          and "await_confirm" in outs.get("tools_done", set())
          and "generate" in outs.get("tools_done", set()),
          str(outs.get("tools_done")))
    check("await_confirm 是终局（不再回到生成）",
          outs.get("await_confirm") == {"__end__"}, str(outs.get("await_confirm")))
    check("generate 后没有再进别的节点（终局）", outs.get("generate") == {"__end__"},
          str(outs.get("generate")))


def test_conditional_after_tools_done() -> None:
    """挂起 → await_confirm；没有挂起 → generate。

    这条分支只会在"真的有人用写工具"时走到。没有它的话，图会在
    pending 为空时也进 await_confirm（用户什么都没做却收到一条确认请求）。
    """
    print("汇合后的分支：有挂起走确认，否则正常生成")
    from app.pipeline.orchestrator import _after_tools_done  # noqa: PLC0415

    check("有挂起 → await_confirm",
          _after_tools_done({"pending": [{"tool": "restart_service"}]}) == "await_confirm")
    check("无挂起 → generate", _after_tools_done({"pending": []}) == "generate")
    check("pending 键缺失（非工具链路）→ generate", _after_tools_done({}) == "generate")


def main() -> int:
    print("=" * 70)
    print("LangGraph 编排结构测试")
    print("=" * 70)
    test_graph_topology()
    test_route_conditional()
    test_cache_and_singleflight_conditionals()
    test_fanout_is_parallel()
    test_conditional_after_tools_done()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
