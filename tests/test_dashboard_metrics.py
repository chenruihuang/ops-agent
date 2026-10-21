"""大盘守门：PromQL 里引用的指标名必须在代码里真实存在。

为什么需要这条
--------------
Grafana 对写错的指标名**不报错**：它只会画一条空线，看起来像"这段时间没流量"。
而"没流量"和"指标名写错了"在排障时是相反的方向。这个错只能靠机器查。

同时钉住两件容易漂的事：
1. 大盘 JSON 必须与生成脚本一致（否则手改一次就再也回不去）；
2. datasource uid 必须与 provisioning 里的一致（否则导入后面板全空）。

D3 追加的两条（都被真实事故打出来的）：
3. **每个指标必须真的有写入点** —— `rag_context_recall` 定义在 metrics.py、
   却从没有任何代码去 set，面板永远空；"永远空"和"没跑过评估"人眼分不出。
4. **每个面板必须设 noValue** —— 空闲时 rate()=0、分位数为 NaN，
   Grafana 渲染成 "No data"，与"PromQL 写错"同形；不写文案就只能靠猜。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "ops-agent.json"
BUILDER = ROOT / "monitoring" / "grafana" / "build_dashboard.py"
METRICS_PY = ROOT / "app" / "observability" / "metrics.py"
JAVA_DIR = ROOT / "java-mcp" / "src" / "main" / "java" / "com" / "opsagent" / "mcp"
DATASOURCE_YML = ROOT / "monitoring" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{'  ' + detail if detail else ''}")


def known_metrics() -> set[str]:
    """从 metrics.py 的 helper 调用里推出 Prometheus 指标名（histogram 要加 _bucket）。"""
    src = METRICS_PY.read_text(encoding="utf-8")
    names: set[str] = set()
    for kind, name in re.findall(r'_(counter|histogram|gauge)\(\s*"([a-z0-9_]+)"', src):
        names.add(name)
        if kind == "histogram":
            names.update({f"{name}_bucket", f"{name}_sum", f"{name}_count"})
    # Java 侧：字符串字面量里写的就是指标名。`Timer` 会自动展开成
    # `_bucket/_count/_sum` 三个时序（percentiles-histogram 打开时），
    # 只登记裸名会让大盘里的 histogram_quantile 被判成"未知指标"。
    for path in JAVA_DIR.glob("*.java"):
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r'"(mcp_[a-z0-9_]+)"', text):
            names.add(name)
            if name.endswith("_seconds"):
                names.update({f"{name}_bucket", f"{name}_sum", f"{name}_count"})
    names.add("up")   # Prometheus 自带的 target 存活指标
    return names


def exprs_of(panel: dict) -> list[str]:
    out = [t.get("expr", "") for t in panel.get("targets", [])]
    for sub in panel.get("panels", []):        # 兼容折叠行
        out += exprs_of(sub)
    return [e for e in out if e]


# 指标名 → 代码里的变量名。大盘只认指标名，而 Python 侧靠变量去 .inc()/.set()，
# 两者之间的唯一桥梁就是 metrics.py 里这行 `NAME = _gauge("name", ...)`。
SYMBOL_RE = re.compile(r'^([A-Z][A-Z0-9_]+)\s*=\s*_(?:counter|histogram|gauge)\(\s*"([a-z0-9_]+)"',
                       re.MULTILINE)
BASE_SUFFIXES = ("_bucket", "_sum", "_count", "_created", "_total")


def writers_of(metric: str) -> int:
    """数一数这个指标在 metrics.py **之外**被引用了多少次（0 = 死指标）。

    先按原名找符号，找不到再去掉 histogram 展开后缀找 —— `ttft_seconds_bucket`
    对应变量 `TTFT_SECONDS`，直接按名字 grep 会误判成"没人写"。
    """
    metrics_src = METRICS_PY.read_text(encoding="utf-8")
    mapping = {m: s for s, m in SYMBOL_RE.findall(metrics_src)}
    candidates = [metric]
    for suffix in ("_bucket", "_sum", "_count", "_created"):
        if metric.endswith(suffix):
            candidates.append(metric[: -len(suffix)])
    symbol = next((mapping[c] for c in candidates if c in mapping), None)

    hits = 0
    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        if path == METRICS_PY:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if symbol:
            hits += text.count(f"{symbol}.")
        else:                       # 没有符号映射（例如 Java 侧字面量）就按名字找
            hits += sum(text.count(f'"{c}"') for c in candidates)
    if metric.startswith("mcp_"):
        for path in JAVA_DIR.glob("*.java"):
            text = path.read_text(encoding="utf-8")
            # Java 侧写的是裸名（`mcp_tool_seconds`），大盘引用的是 `_bucket`
            hits += sum(text.count(f'"{c}"') for c in candidates)
    return hits


def main() -> int:
    print("=" * 70)
    print("Grafana 大盘守门")
    print("=" * 70)

    check("大盘 JSON 存在", DASHBOARD.exists(), str(DASHBOARD))
    if not DASHBOARD.exists():
        return 1
    data = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    check("是合法 JSON 且含 panels", isinstance(data.get("panels"), list) and data["panels"],
          str(type(data.get("panels"))))

    # 1) 与生成脚本一致（防止手改后漂移）
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_dashboard", BUILDER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    generated = json.dumps(mod.build(), ensure_ascii=False, indent=2).strip()
    actual = json.dumps(data, ensure_ascii=False, indent=2).strip()
    check("大盘与生成脚本一致（不可手改）", generated == actual,
          "跑 python -m monitoring.grafana.build_dashboard 重新生成")

    # 2) 指标名必须存在
    known = known_metrics()
    check("从 metrics.py / Java 侧解析出指标名（>10 个）", len(known) > 10, f"{len(known)} 个")
    print(f"    已知指标：{', '.join(sorted(known)[:6])} …（共 {len(known)}）")

    used: set[str] = set()
    for panel in data["panels"]:
        for expr in exprs_of(panel):
            for token in re.findall(r"\b([a-z][a-z0-9_]{3,})\b", expr):
                if token in known or token.endswith(("_total", "_bucket", "_seconds", "_state")):
                    used.add(token)
    unknown = sorted(t for t in used if t not in known)
    check("表达式里的指标名全部存在", not unknown, f"未知指标：{unknown}")
    check("大盘至少引用了 8 个指标", len(used) >= 8, f"{len(used)} 个")

    # 2b) 只定义、不写入 = 死面板：它永远空，而"永远空"看起来像"没跑过"
    dead = sorted(m for m in used if m != "up" and not writers_of(m))
    check("每个被引用的指标都有写入点", not dead, f"无写入点（死面板）：{dead}")

    # 2c) 空态必须说人话，否则与"PromQL 写错"无法区分
    no_value_missing = [
        p.get("title") for p in data["panels"]
        if p.get("type") != "row"
        and not (p.get("fieldConfig") or {}).get("defaults", {}).get("noValue")
    ]
    check("非行面板都设了 noValue", not no_value_missing, str(no_value_missing))

    # 3) 数据源 uid 与 provisioning 一致
    yml = DATASOURCE_YML.read_text(encoding="utf-8")
    check("provisioning 固定了 uid", "uid: prometheus" in yml)
    uids = {t.get("datasource", {}).get("uid") for p in data["panels"] for t in p.get("targets", [])}
    check("面板引用的 uid 与 provisioning 一致", uids == {"prometheus"}, str(uids))

    # 4) 每个面板都有一句人话（否则看图的人只能猜）
    missing = [p.get("title") for p in data["panels"]
               if p.get("type") != "row" and not p.get("description")]
    check("非行面板都带 description", not missing, str(missing))

    print(f"\n{PASS} 条通过 · {FAIL} 条失败")
    return 1 if FAIL else 0


def test_dashboard_guard() -> None:
    """pytest 入口：真正把关的是 check() 累加 + conftest 的退出码兜底。"""
    main()


if __name__ == "__main__":
    raise SystemExit(main())
