"""生成 Grafana 大盘 JSON。

为什么不手写 JSON
-----------------
面板里的 PromQL 是**唯一真相**：指标名写错一个字母，Grafana 不会报错，
只会画一条空线。而 `app/observability/metrics.py` 是这些名字的**唯一来源**。
所以这里把 expr 写成常量、由脚本生成 JSON，再用
`tests/test_dashboard_metrics.py` 逐条核对"表达式中出现的指标名必须在代码里存在"。

跑法
----
    python -m monitoring.grafana.build_dashboard          # 生成/覆盖 dashboards/ops-agent.json
    python -m monitoring.grafana.build_dashboard --check   # 只校验现有 JSON 是否过期（CI 用）

刻意不做的事
------------
不做"五大指标自动打分"这种面板：可用性、MTTR 来自 `scripts/fault_inject.py` 的
实验（T2/T3 时间戳），Prometheus 里没有这两个数。把实验结论 P 到面板上
就是编数据 —— 面板只画**真实采集到的**时序，实验结论写进报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("dashboards") / "ops-agent.json"
UID = "prometheus"


def _target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {
        "datasource": {"type": "prometheus", "uid": UID},
        "expr": expr,
        "refId": ref,
        "legendFormat": legend,
    }
    if instant:
        t["instant"] = True
        t["range"] = False
    return t


def _panel(
    *,
    title: str,
    ptype: str,
    grid: tuple[int, int, int, int],
    targets: list[dict[str, Any]],
    unit: str = "short",
    description: str = "",
    no_value: str = "无流量（窗口内没有请求）",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    x, y, w, h = grid
    panel: dict[str, Any] = {
        "id": 0,
        "title": title,
        "type": ptype,
        "datasource": {"type": "prometheus", "uid": UID},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {
            # noValue 不是装饰：没流量时 rate()=0、histogram_quantile=NaN，
            # Grafana 会把**整块面板**渲染成 "No data" —— 和"指标名写错了"长得一模一样。
            # 明确写"无流量"，排障方向就只剩一个。
            "defaults": {"unit": unit, "noValue": no_value},
            "overrides": [],
        },
        "options": {},
    }
    if description:
        panel["description"] = description
    if extra:
        panel.update(extra)
    return panel


def _row(title: str, y: int) -> dict[str, Any]:
    return {
        "id": 0,
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


def build() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []

    # ---------------- ⓪ 自检：盘空先看这里 ----------------
    # 这张大盘 90% 的面板是 rate()/histogram_quantile()：机器一空闲，
    # rate 全为 0、分位数为 NaN，整盘会显示成 "No data" —— 而 "No data" 与
    # "PromQL 写错了" 在视觉上**完全一样**。把"有没有流量"单独摆在最上面，
    # 排障第一步就变成一次目视，而不是去翻 PromQL。
    head: list[dict[str, Any]] = [
        _row("⓪ 盘空先看这里：窗口内到底有没有流量", 0),
        _panel(
            title="近 5 分钟请求数",
            ptype="stat",
            grid=(0, 1, 5, 4),
            unit="short",
            description="0 = 最近 5 分钟没有请求，下面所有 rate/分位数面板本来就该是空的。"
                        "先发一条请求再判断「大盘坏了」。",
            no_value="无流量",
            targets=[_target("sum(increase(chat_requests_total[5m]))", "请求数", instant=True)],
            extra={"options": {"reduceOptions": {"calcs": ["lastNotNull"]}}},
        ),
        _panel(
            title="近 1 小时请求数",
            ptype="stat",
            grid=(5, 1, 5, 4),
            unit="short",
            description="用来区分「刚空闲」和「一直没起来」：1 小时有数但 5 分钟没数 = 只是空闲。",
            no_value="无流量",
            targets=[_target("sum(increase(chat_requests_total[1h]))", "请求数", instant=True)],
            extra={"options": {"reduceOptions": {"calcs": ["lastNotNull"]}}},
        ),
        _panel(
            title="请求活动时间线",
            ptype="timeseries",
            grid=(10, 1, 14, 4),
            unit="short",
            description="每条 = 该时刻最近 5 分钟的请求数。柱子出现在什么时候，"
                        "就把时间窗拉到那一段，而不是怀疑面板。",
            no_value="无流量",
            targets=[_target("sum(increase(chat_requests_total[5m]))", "近 5 分钟请求数")],
        ),
    ]

    # ---------------- ① 请求与延迟 ----------------
    panels.append(_row("① 请求与延迟（TTFT / 分阶段）", 0))
    panels.append(_panel(
        title="请求成功率",
        ptype="stat",
        grid=(0, 1, 6, 5),
        unit="percentunit",
        description="口径：LLM 调用成功数 / 全部调用数。"
                    "这不是 SLA —— 真实可用性看故障注入实验（scripts/fault_inject.py）。"
                    "分母为 0 时这里显示「无流量」而不是 0%：0/0 是 NaN，"
                    "曾经用 clamp_min(…,1e-9) 兜底，结果空闲时面板写着「0% 成功率」——"
                    "一个会让所有人误判 AI 挂了的数字。",
        no_value="无流量",
        targets=[_target(
            # 刻意不 clamp 分母：0/0 → NaN → Grafana 判为无数据 → 走 noValue 文案。
            # 「没发生」和「发生了且失败」必须能分开。
            'sum(rate(chat_requests_total{result="ok"}[5m])) '
            '/ sum(rate(chat_requests_total[5m]))',
            "成功率", instant=True,
        )],
        extra={"options": {"reduceOptions": {"calcs": ["lastNotNull"]}}},
    ))
    panels.append(_panel(
        title="吞吐（LLM 调用/秒）",
        ptype="timeseries",
        grid=(6, 1, 6, 5),
        unit="reqps",
        description="按 model 分。压测时它和 Locust 的 RPS 对照，能看出排队发生在哪一侧。",
        targets=[_target('sum by (model) (rate(llm_requests_total[5m]))', "{{model}}")],
    ))
    panels.append(_panel(
        title="TTFT p50 / p95",
        ptype="timeseries",
        grid=(12, 1, 6, 5),
        unit="s",
        description="只看首 token。缓存命中的请求会天然更快，所以按 cache 标签分开画 —— "
                    "混在一起会把缓存收益误读成模型变快。",
        targets=[
            _target('histogram_quantile(0.5, sum by (le, cache) '
                    '(rate(ttft_seconds_bucket[5m])))', "p50 cache={{cache}}"),
            _target('histogram_quantile(0.95, sum by (le, cache) '
                    '(rate(ttft_seconds_bucket[5m])))', "p95 cache={{cache}}", ref="B"),
        ],
    ))
    panels.append(_panel(
        title="分阶段 P95（定位劣化出在哪一段）",
        ptype="timeseries",
        grid=(18, 1, 6, 5),
        unit="s",
        description="按阶段拆开。看「是哪一段变慢」比看总耗时有用得多 —— "
                    "总耗时劣化时，这里通常只有一段在涨。",
        targets=[_target('histogram_quantile(0.95, sum by (le, stage) '
                         '(rate(stage_seconds_bucket[5m])))', "{{stage}}")],
    ))

    # ---------------- ② 缓存 ----------------
    panels.append(_row("② 缓存（命中率与护栏）", 6))
    panels.append(_panel(
        title="缓存命中率（按层）",
        ptype="timeseries",
        grid=(0, 7, 8, 6),
        unit="percentunit",
        description="hit / (hit+miss)。rejected 是护栏拒绝，不是 miss —— "
                    "把它算进 miss 会让阈值调优的效果看不出来。"
                    "分母为 0 时显示「无流量（无缓存查询）」，不是 0% 命中率。",
        no_value="无缓存查询",
        targets=[_target(
            'sum by (layer) (rate(cache_requests_total{result="hit"}[5m])) '
            '/ sum by (layer) (rate(cache_requests_total{result=~"hit|miss"}[5m]))',
            "{{layer}}",
        )],
    ))
    panels.append(_panel(
        title="语义缓存候选相似度分布",
        ptype="timeseries",
        grid=(8, 7, 8, 6),
        unit="short",
        description="用来标定阈值：如果 p50 贴着阈值，说明阈值在误命中边缘。",
        targets=[
            _target('histogram_quantile(0.5, sum by (le) '
                    '(rate(cache_semantic_similarity_bucket[5m])))', "p50"),
            _target('histogram_quantile(0.9, sum by (le) '
                    '(rate(cache_semantic_similarity_bucket[5m])))', "p90", ref="B"),
        ],
    ))
    panels.append(_panel(
        title="护栏拒绝（按原因）",
        ptype="timeseries",
        grid=(16, 7, 8, 6),
        unit="short",
        description="护栏拒绝 = 向量很像但实体对不上（工单号/错误码不同）。"
                    "它涨说明阈值在诱捕，不是缓存变差了。",
        targets=[_target('sum by (reason) (rate(cache_guard_rejected_total[5m]))', "{{reason}}")],
    ))
    panels.append(_panel(
        title="缓存省下的 token（近 1 小时）",
        ptype="stat",
        grid=(0, 13, 6, 4),
        unit="short",
        description="成本指标。命中一次就省一次完整 prompt 的 token —— "
                    "报「缓存有用」时用它，而不是只报命中率。",
        targets=[_target("sum(increase(cache_tokens_saved_total[1h]))", "tokens", instant=True)],
    ))

    # ---------------- ③ 高可用 ----------------
    panels.append(_row("③ 高可用（熔断 / 重试 / 降级）", 13))
    panels.append(_panel(
        title="熔断器状态",
        ptype="state-timeline",
        grid=(6, 13, 6, 4),
        unit="short",
        description="0=closed 1=half_open 2=open。状态切换是事故强信号，"
                    "WARNING 日志同步打点。",
        targets=[_target("circuit_breaker_state", "{{dependency}}/{{state}}")],
    ))
    panels.append(_panel(
        title="工具结果来源（mcp / cache / mock）",
        ptype="timeseries",
        grid=(12, 13, 6, 4),
        unit="reqps",
        description="mock 占比上来通常意味着 MCP 不健康 —— 而不是「工具就是慢」。",
        targets=[_target("sum by (source) (rate(tool_calls_total[5m]))", "{{source}}")],
    ))
    panels.append(_panel(
        title="重试速率",
        ptype="timeseries",
        grid=(18, 13, 6, 4),
        unit="short",
        description="重试是在「吃掉抖动」，但持续高位意味着依赖一直不健康 —— "
                    "配合熔断状态一起看才有意义。",
        targets=[_target("sum by (dependency) (rate(tool_retries_total[5m]))", "{{dependency}}")],
    ))
    panels.append(_panel(
        title="降级跳数（为什么降级）",
        ptype="timeseries",
        grid=(0, 17, 12, 5),
        unit="short",
        description="reason: circuit_open / unavailable / timeout / error:Type。"
                    "排障方向完全不同，必须分开看。",
        targets=[_target('sum by (stage, reason) (rate(tool_degraded_total[5m]))',
                         "{{stage}}/{{reason}}")],
    ))
    panels.append(_panel(
        title="降级应答（按阶段）",
        ptype="timeseries",
        grid=(12, 17, 12, 5),
        unit="short",
        description="按 stage 分（recall / tools / memory / generate）。"
                    "降级必须是「用户看得见、我们能追」的，不是静默降智。",
        targets=[_target('sum by (stage, reason) (rate(degraded_responses_total[5m]))',
                         "{{stage}}/{{reason}}")],
    ))

    # ---------------- ④ RAG 质量与 Java 工具服务 ----------------
    panels.append(_row("④ RAG 质量（评估产物）与 Java MCP", 22))
    panels.append(_panel(
        title="上下文召回率 / 精度（评估产物）",
        ptype="stat",
        grid=(0, 23, 8, 5),
        unit="percentunit",
        description="由 scripts/run_eval --publish 经 POST /admin/eval/metrics 写进 api 进程。"
                    "为空 = 还没跑过**全量**评估（抽样结果刻意不写大盘，否则 8 题的 0.9 会盖掉"
                    "48 题的 0.8785）。faithfulness 需要 ragas 判定，本项目不产出，"
                    "所以这里不放它的格子 —— 少一块比放一个永远空的格子诚实。",
        no_value="未跑过评估",
        targets=[
            _target("max(rag_context_recall)", "recall@{{top_k}}", instant=True),
            _target("max(rag_context_precision)", "precision@{{top_k}}", ref="B", instant=True),
            _target("max(rag_anchor_hit_rate)", "anchor@{{top_k}}", ref="C", instant=True),
        ],
        extra={"options": {"reduceOptions": {"calcs": ["lastNotNull"]}}},
    ))
    panels.append(_panel(
        title="Java MCP 工具 QPS",
        ptype="timeseries",
        grid=(8, 23, 8, 5),
        unit="reqps",
        description="按工具分。与 Python 侧 tool_calls_total{source=\"mcp\"} 应对得上 —— "
                    "对不上说明有调用没走 MCP（降级或缓存命中）。",
        targets=[_target("sum by (tool) (rate(mcp_tool_calls_total[5m]))", "{{tool}}")],
    ))
    panels.append(_panel(
        title="Java MCP 工具 P95",
        ptype="timeseries",
        grid=(16, 23, 8, 5),
        unit="s",
        description="Actuator + Micrometer 直方图。注入 sleep/hang 时这里会立刻抬头，"
                    "是验证 chaos 生效最快的证据。",
        targets=[_target('histogram_quantile(0.95, sum by (le, tool) '
                         '(rate(mcp_tool_seconds_bucket[5m])))', "{{tool}}")],
    ))
    panels.append(_panel(
        title="故障注入次数（X-Chaos）",
        ptype="timeseries",
        grid=(0, 28, 8, 4),
        unit="short",
        description="做 5 轮故障实验时，用它核对「注入真的发生了」以及次数是否对得上。",
        targets=[_target("sum by (mode) (rate(mcp_chaos_total[10m]))", "{{mode}}")],
    ))
    panels.append(_panel(
        title="抓取目标存活",
        ptype="stat",
        grid=(8, 28, 8, 4),
        unit="short",
        description="up=0 意味着这个 job 的指标整块缺失。"
                    "java-mcp 仍为 0 时先看 Actuator 是否暴露 /actuator/prometheus。",
        no_value="抓不到目标",
        targets=[_target('up{job=~"ops-api|java-mcp"}', "{{job}}", instant=True)],
        extra={"options": {"reduceOptions": {"calcs": ["lastNotNull"]}}},
    ))
    panels.append(_panel(
        title="降级实验：成功率 vs 重试",
        ptype="timeseries",
        grid=(16, 28, 8, 4),
        unit="short",
        description="做故障注入时看这两条：失败率抬头而重试同步抬头 = 在吃抖动；"
                    "失败率抬头但重试没动 = 熔断已打开（快速失败，不再浪费重试）。",
        targets=[
            _target('sum(rate(llm_requests_total{result!="ok"}[5m]))', "失败/秒"),
            _target("sum(rate(tool_retries_total[5m]))", "重试/秒", ref="B"),
        ],
    ))

    # ---------------- ⑤ 安全沙箱 ----------------
    # 为什么沙箱也要有面板：它最典型的失效方式是**静默的** ——
    # 写了确认逻辑，但写工具的 risk 标成了 read，于是每次重启都直接执行、
    # 一次确认都没弹过。这种"看起来一切正常"的状态只能靠 denied/pending
    # 的计数为 0 来发现：有写工具却没有 pending，说明闸门没接上。
    panels.append(_row("⑤ 安全沙箱（写操作的人工确认）", 33))
    panels.append(_panel(
        title="沙箱决策（5 分钟）",
        ptype="timeseries",
        grid=(0, 34, 8, 4),
        unit="short",
        description="pending / approved / rejected / denied / invalid_grant。"
                    "做一次「重启 order-service」应当看到 1 次 pending + 1 次 approved；"
                    "只有 approved 没有 pending = 闸门被绕过（tool 的 risk 没标）。",
        no_value="窗口内没有写操作（只读工具不计数）",
        targets=[_target("sum by (decision) (rate(sandbox_decisions_total[5m]))",
                         "{{decision}}")],
    ))
    panels.append(_panel(
        title="待确认动作：从挂起到人点确认的时长 P95",
        ptype="timeseries",
        grid=(8, 34, 8, 4),
        unit="s",
        description="这个数衡量的是**人**，不是系统。几十分钟说明请求根本没人看 —— "
                    "这时优化确认接口的耗时没有任何意义。",
        no_value="窗口内没有确认动作",
        targets=[_target("histogram_quantile(0.95, sum by (le) "
                         "(rate(sandbox_confirm_seconds_bucket[10m])))", "P95")],
    ))
    panels.append(_panel(
        title="被策略拒绝的调用（denied / invalid_grant）",
        ptype="stat",
        grid=(16, 34, 8, 4),
        unit="short",
        description="denied = 参数越界或动作被禁用（有人在探边界，要查是谁在调）；"
                    "invalid_grant = 凭据过期/重放/挪用（正常流程也会有，多为超时）。"
                    "两者都不代表系统故障，所以**不计入** degraded 指标。"
                    "右侧「在册写工具」是对照值：它 > 0 而 pending 恒为 0 时，"
                    "说明闸门没接上（risk 标错），而不是没人用过写操作。",
        no_value="窗口内没有拒绝",
        targets=[
            _target('sum(increase(sandbox_decisions_total{decision="denied"}[$__range]))',
                    "denied"),
            _target('sum(increase(sandbox_decisions_total{decision="invalid_grant"}'
                    '[$__range]))', "invalid_grant", ref="B"),
            _target("max(sandbox_write_tools)", "在册写工具", ref="C", instant=True),
        ],
    ))

    # ⓪ 行占了 y=0..5，把其余面板整体下移，避免两张行卡片叠在同一格
    for panel in panels:
        panel["gridPos"]["y"] += 5
    panels = head + panels

    for i, panel in enumerate(panels, start=1):
        panel["id"] = i

    return {
        "uid": "ops-agent-main",
        "title": "Ops Agent · 主链路大盘",
        "tags": ["ops-agent", "rag", "availability"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        # 默认看 3 小时而不是 30 分钟：这个项目是「跑一段、看一段」的用法，
        # 30 分钟窗口会在空闲后变成整盘空白，看起来像坏了。
        "time": {"from": "now-3h", "to": "now"},
        "editable": True,
        "panels": panels,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只比对现有 JSON 是否与生成器一致")
    args = parser.parse_args(argv)

    payload = json.dumps(build(), ensure_ascii=False, indent=2)
    if args.check:
        if not OUT.exists():
            print(f"[FAIL] 缺少 {OUT}", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8").strip() != payload.strip():
            print("[FAIL] 大盘 JSON 与生成器不一致 —— 跑 python -m monitoring.grafana.build_dashboard 重新生成",
                  file=sys.stderr)
            return 1
        print("[OK] 大盘 JSON 与生成器一致")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(payload + "\n", encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))   # 自校验：产物必须是合法 JSON
    print(f"[OK] 已生成 {OUT}（{len(build()['panels'])} 个面板/行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
