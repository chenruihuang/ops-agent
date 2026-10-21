"""会话回放：把 JSONL 里的 span 还原成一次对话的时间线。

为什么要有这个脚本
------------------
Langfuse 的 UI 很好，但它有两个前提：有 key、有网。而这两条在**排障现场**
经常不成立 —— 出问题的时候，你恰好不想先登录一个云端控制台。

本地 JSONL 是埋点的全量事实源（见 app/observability/tracing.py 的取舍说明），
这个脚本负责把它变成人能读的东西：

    python -m scripts.trace_replay --last 3
    python -m scripts.trace_replay --session sess-6a6b9d39 --gantt
    python -m scripts.trace_replay --session sess-6a6b9d39 --md replay.md

三个用途，按重要性排：

1) **复现一次慢请求**：哪一段吃了多少毫秒、TTFT 落在生成段的第几毫秒。
2) **验证埋点没漏**：阶段数、父子关系、TTFT 是否缺 —— 少一段一眼能看出来。
3) **面试/复盘素材**：`--md` 导出的表格可以直接贴进文档。

它不是日志查看器：只读 `data/traces/*.jsonl`，不改任何东西。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.observability.tracing import iter_trace_records  # noqa: E402
from scripts.selftest import reconcile_stdout                # noqa: E402

# 甘特条宽度（字符）。40 在 100 列的终端里放得下，且 1ms~10s 都能看出形状。
BAR_WIDTH = 40

# 阶段缩写：终端里列宽有限，`cache_lookup` 会把整行挤歪
SHORT = {
    "turn": "turn",
    "route": "route",
    "memory_load": "mem_load",
    "memory_save": "mem_save",
    "embed": "embed",
    "cache_lookup": "cache",
    "cache_store": "cache_w",
    "singleflight": "singlefly",
    "tools": "tools",
    "recall": "recall",
    "rerank": "rerank",
    "generate": "generate",
    "chitchat": "chitchat",
}


# =====================================================================
# 读取与聚合
# =====================================================================
def trace_files(directory: str | Path) -> list[Path]:
    return sorted(Path(directory).glob("*.jsonl"))


def load_records(directory: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in trace_files(directory):
        out.extend(iter_trace_records(path))
    return out


def group_by_trace(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        tid = rec.get("trace_id")
        if tid:
            groups.setdefault(tid, []).append(rec)
    # 组内按 start_ms 排序：turn 是 0，天然在最前
    for rows in groups.values():
        rows.sort(key=lambda r: (r.get("start_ms") or 0.0))
    return groups


def turn_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next((r for r in rows if r.get("stage") == "turn"), {})


def sessions_index(groups: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """session_id → [trace_id...]，按 turn 的时间排。"""
    index: dict[str, list[tuple[float, str]]] = {}
    for tid, rows in groups.items():
        turn = turn_of(rows)
        sid = turn.get("session_id") or rows[0].get("session_id") or ""
        index.setdefault(sid, []).append((turn.get("start_epoch") or 0.0, tid))
    return {sid: [tid for _, tid in sorted(items)] for sid, items in index.items()}


# =====================================================================
# 渲染
# =====================================================================
def short(stage: str) -> str:
    return SHORT.get(stage, stage[:10])


def gantt(start_ms: float, duration_ms: float, total_ms: float) -> str:
    """在总时长上画一根条。总时长为 0 时给一个固定占位，避免除零。"""
    if total_ms <= 0:
        return "|"
    total_ms = max(total_ms, 1.0)
    head = int(round((start_ms / total_ms) * BAR_WIDTH))
    span = int(round((duration_ms / total_ms) * BAR_WIDTH))
    head = max(0, min(BAR_WIDTH - 1, head))
    span = max(1, min(BAR_WIDTH - head, span))
    return " " * head + "#" * span + " " * (BAR_WIDTH - head - span)


def _fmt_ms(value: Any) -> str:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.2f}s"


def _meta(turn: dict[str, Any]) -> dict[str, Any]:
    return turn.get("metadata") or {}


def render_trace(rows: list[dict[str, Any]], *, gantt_chart: bool = True,
                 answer_chars: int = 120) -> list[str]:
    turn = turn_of(rows)
    meta = _meta(turn)
    total = float(turn.get("duration_ms") or 0.0)
    ttft = meta.get("ttft_ms")
    degraded = meta.get("degraded") or []

    head = (
        f"第 {datetime.fromtimestamp(turn.get('start_epoch') or 0).strftime('%H:%M:%S')} 轮"
        f"  trace {str(turn.get('trace_id', ''))[:8]}"
        f"  total {_fmt_ms(total)}"
        f"  TTFT {_fmt_ms(ttft) if ttft is not None else '-'}"
        f"  route={meta.get('route') or '-'}"
        f"  cache={meta.get('cache') or '-'}"
    )
    lines = [head]
    if degraded:
        lines.append(f"  降级：{', '.join(str(d) for d in degraded)}")

    lines.append("  阶段：")
    for rec in rows:
        if rec.get("stage") == "turn":
            continue
        stage = short(str(rec.get("stage")))
        level = rec.get("level")
        mark = " " if level in (None, "DEFAULT") else "!"
        bar = ("  " + gantt(float(rec.get("start_ms") or 0.0),
                            float(rec.get("duration_ms") or 0.0), total)) if gantt_chart else ""
        # 降级必须带原因：只标一个 `!` 的话，`rerank:disabled`（配置的必然结果）
        # 和 `recall:TimeoutError`（真故障）在屏幕上长得一模一样。
        cause = f"  <- {rec.get('status_message')}" if mark == "!" and rec.get("status_message") else ""
        lines.append(
            f"   {mark} {stage:<9} start {_fmt_ms(rec.get('start_ms')):>8}"
            f"  dur {_fmt_ms(rec.get('duration_ms')):>8}{bar}{cause}"
        )

    # 关键事实：检索来源 / 工具 / 缓存命中，回放的价值主要在这三行
    recall = next((r for r in rows if r.get("stage") == "recall"), {})
    out = recall.get("output") or {}
    if out.get("top_sources"):
        lines.append(f"  依据：{', '.join(str(s) for s in out['top_sources'][:5])}")
    if out.get("order_by"):
        lines.append(f"  排序口径：{out['order_by']}（rank_scores={out.get('rank_scores')}）")

    tools = next((r for r in rows if r.get("stage") == "tools"), {})
    tool_rows = ((tools.get("output") or {}).get("tools")) or []
    for item in tool_rows:
        flag = "ok" if item.get("ok") else "fail"
        lines.append(f"  工具：{item.get('name')}({flag}, {_fmt_ms(item.get('elapsed_ms'))})")

    gen = next((r for r in rows if r.get("stage") == "generate"), {})
    gmeta = gen.get("metadata") or {}
    if gmeta.get("first_token_offset_ms") is not None:
        lines.append(
            f"  生成：首字在生成段第 {_fmt_ms(gmeta['first_token_offset_ms'])}"
            f"（占 turn 的 {_fmt_ms(gmeta.get('ttft_ms'))}）"
        )

    answer = (turn.get("output") or {}).get("answer") or ""
    if answer:
        clipped = answer.replace("\n", " ")[:answer_chars]
        suffix = "…" if len(answer) > answer_chars else ""
        lines.append(f"  答案（{len(answer)} 字）：{clipped}{suffix}")
    return lines


def render_session(session_id: str, trace_ids: list[str],
                   groups: dict[str, list[dict[str, Any]]], *,
                   gantt_chart: bool = True) -> str:
    lines = [
        "=" * 72,
        f"会话回放 · session={session_id or '(无)'} · {len(trace_ids)} 轮",
        "=" * 72,
    ]
    for tid in trace_ids:
        lines.extend(render_trace(groups[tid], gantt_chart=gantt_chart))
        lines.append("")
    if len(trace_ids) > 1:
        lines.append(f"合计 {len(trace_ids)} 轮 · 首轮 {_fmt_ms(total_of(groups[trace_ids[0]]))}"
                     f" · 末轮 {_fmt_ms(total_of(groups[trace_ids[-1]]))}")
    return "\n".join(lines)


def total_of(rows: list[dict[str, Any]]) -> float:
    return float(turn_of(rows).get("duration_ms") or 0.0)


def to_markdown(session_id: str, trace_ids: list[str],
                groups: dict[str, list[dict[str, Any]]]) -> str:
    lines = [
        f"# 会话回放 · `{session_id or '(无)'}`",
        "",
        f"- 轮数：{len(trace_ids)}",
        f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| # | 时间 | trace | route | cache | TTFT | total | 阶段耗时 | 降级 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, tid in enumerate(trace_ids, 1):
        rows = groups[tid]
        turn = turn_of(rows)
        meta = _meta(turn)
        stages = " · ".join(
            f"{short(str(r.get('stage')))} {_fmt_ms(r.get('duration_ms'))}"
            for r in rows if r.get("stage") != "turn"
        )
        lines.append(
            f"| {i} | {datetime.fromtimestamp(turn.get('start_epoch') or 0).strftime('%H:%M:%S')} "
            f"| `{tid[:8]}` | {meta.get('route') or '-'} | {meta.get('cache') or '-'} "
            f"| {_fmt_ms(meta.get('ttft_ms'))} | {_fmt_ms(turn.get('duration_ms'))} "
            f"| {stages or '-'} | {', '.join(meta.get('degraded') or []) or '-'} |"
        )
    lines.append("")
    lines.append("## 逐轮明细")
    for i, tid in enumerate(trace_ids, 1):
        turn = turn_of(groups[tid])
        lines.append("")
        lines.append(f"### 第 {i} 轮 · `{tid[:8]}`")
        lines.append("")
        lines.append(f"- 问题：{(turn.get('input') or {}).get('message', '')}")
        lines.append(f"- 答案：{((turn.get('output') or {}).get('answer') or '')[:200]}")
        lines.append("")
        lines.append("```")
        lines.extend(render_trace(groups[tid], gantt_chart=False, answer_chars=0))
        lines.append("```")
    return "\n".join(lines)


# =====================================================================
# CLI
# =====================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trace_replay",
        description="从本地 JSONL 回放一次对话的完整时间线（无需 Langfuse）",
    )
    p.add_argument("--dir", default="", help="trace 目录（默认取 OBSERVABILITY_TRACE_DIR / data/traces）")
    p.add_argument("--session", default="", help="按 session_id 回放（多轮串起来）")
    p.add_argument("--trace", default="", help="按 trace_id 前缀回放单轮")
    p.add_argument("--last", type=int, default=0, help="最近 N 轮（默认 1）")
    p.add_argument("--list-sessions", action="store_true", help="列出所有会话及轮数")
    p.add_argument("--no-gantt", action="store_true", help="不画甘特条（贴日志时更干净）")
    p.add_argument("--md", default="", help="导出 markdown 到指定文件")
    p.add_argument("--json", action="store_true", help="输出原始 JSON（给脚本消费）")
    return p


def resolve_dir(raw: str) -> Path:
    if raw:
        return Path(raw)
    try:
        from app.core.config import settings

        return Path(settings.observability_trace_dir or "data/traces")
    except Exception:  # noqa: BLE001 - 配置读不到就用默认
        return Path("data/traces")


def main(argv: list[str] | None = None) -> int:
    note = reconcile_stdout()
    if note:
        print(note)
    args = build_parser().parse_args(argv)

    directory = resolve_dir(args.dir)
    if not directory.exists():
        print(f"trace 目录不存在：{directory}")
        print("先跑一次请求（或 .\\scripts\\selftest.ps1 -Only trace），再回放。")
        return 2

    records = load_records(directory)
    if not records:
        print(f"{directory} 里没有 trace 记录。")
        print("排查：OBSERVABILITY_ENABLED 是否为 false；服务是否刚重启（内存里没有历史）。")
        return 2

    groups = group_by_trace(records)
    sessions = sessions_index(groups)

    if args.list_sessions:
        for sid, tids in sessions.items():
            when = datetime.fromtimestamp(
                turn_of(groups[tids[-1]]).get("start_epoch") or 0
            ).strftime("%Y-%m-%d %H:%M")
            print(f"  {sid or '(无 session)':<28} {len(tids):>3} 轮   最近 {when}")
        return 0

    if args.session:
        trace_ids = sessions.get(args.session, [])
        if not trace_ids:
            print(f"没有 session={args.session} 的记录。已有会话：")
            for sid in list(sessions)[:10]:
                print(f"  {sid or '(无 session)'}")
            return 2
        title = args.session
    elif args.trace:
        trace_ids = [tid for tid in groups if tid.startswith(args.trace)]
        if not trace_ids:
            print(f"没有 trace_id 以 {args.trace} 开头的记录")
            return 2
        title = f"trace:{args.trace}"
    else:
        # 默认最近 N 轮（跨会话，按 turn 起始时间倒序）
        ordered = sorted(
            groups, key=lambda t: turn_of(groups[t]).get("start_epoch") or 0.0, reverse=True
        )
        want = args.last or 1
        trace_ids = list(reversed(ordered[:want]))
        title = f"最近 {len(trace_ids)} 轮"

    if args.json:
        payload = {tid: groups[tid] for tid in trace_ids}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.md:
        body = to_markdown(title, trace_ids, groups)
        Path(args.md).write_text(body, encoding="utf-8")
        print(f"已导出 markdown：{args.md}（{len(trace_ids)} 轮）")
        return 0

    if args.session:
        print(render_session(args.session, trace_ids, groups, gantt_chart=not args.no_gantt))
    else:
        lines = ["=" * 72, f"回放 · {title}", "=" * 72]
        for tid in trace_ids:
            lines.extend(render_trace(groups[tid], gantt_chart=not args.no_gantt))
            lines.append("")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
