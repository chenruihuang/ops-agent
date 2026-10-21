"""D1 自测：一条命令跑完「LangGraph 编排 + 三层记忆」的接口级验证。

为什么要写这个脚本，而不是在文档里给人一串 curl
------------------------------------------------
Windows PowerShell 5.1 把含中文的 JSON 内联在命令行里是不可靠的，实测：

    形式                              传给原生程序的实际字节
    "{\\"message\\":\\"VPN 连不上\\"}"     {\\                （双引号被当字符串结束符截断）
    '{"message":"hi"}'                {message:hi}      （PowerShell 重组成 argv 时剥掉了引号）
    '{\\"message\\":\\"hi\\"}'              {"message":"hi"}  （唯一正确的形式）
    再加中文：'{\\"message\\":\\"VPN 连不上\\"}' → {"message":"VPN   （中文字节丢失）

也就是说"照着文档敲命令"这件事本身就有失败率，而**测试脚本报的错会被
误读成服务有问题** —— 这是最坏的一种假信号。所以把请求内建成脚本，
命令行只出现 ASCII 参数（场景名 / session id）。

第二个坑：开发机/CI 常设 `HTTP_PROXY`（本机实测就有），httpx 默认
`trust_env=True` 会把它读进来，于是 http://localhost:8080 的请求被送去
代理，回来一个 **502** —— 这个 502 看起来像"网关后面的服务挂了"，
实际上服务可能压根没起，或者根本没被访问到。所以这里**默认直连**
（`trust_env=False`）；确需走代理时加 `--use-proxy`。

判定标准
--------
不只打印，还给 PASS/FAIL 与退出码。检查项对齐 README「SSE 事件协议」与
「LangGraph 编排与三层记忆」两节里承诺的性质：

    · meta 必须是第一帧（前端拿它决定后续渲染）
    · 召回结果要有 docs 帧（rag 链路）
    · done 必须是最后一帧（之后不再消费）
    · 记忆失效必须可见（stage=memory），且不影响回答完成
    · 第一轮无历史是**正常**的（不该被当成 bug）

用法
----
    # 服务地址默认 http://localhost:8080（经 nginx），key 自动从 .env 读
    python -m scripts.selftest
    python -m scripts.selftest --only route,stream      # 只跑部分场景
    python -m scripts.selftest --list                   # 列出场景

    # 记忆降级场景需要先把 redis 停掉（脚本不替你停容器）
    docker stop ops-redis
    python -m scripts.selftest --only memory_down
    docker start ops-redis

    # 干净日志（UTF-8，绕开终端编码）
    python -m scripts.selftest --log report.log

输出编码
--------
Windows + PS 5.1 的终端会按码页（936/GBK）解码子进程输出，而本机环境里有
`PYTHONUTF8=1`，于是 UTF-8 字节被当成 GBK 读 → `D1 鑷祴`。脚本启动时会
**自己纠回来**（见 reconcile_stdout）：管道（如 `Tee-Object`）+ 父进程是
powershell.exe / cmd.exe 时，把 stdout 降级到码页编码，emoji 勾叉换成 ASCII；
父进程是 pwsh 7 / bash 时不动；**直连控制台也不动** —— 那条路径 Python 用
WriteConsoleW 宽字符直写，中文本来就正常，reconfigure 反而会把它改坏
（trace_doctor 乱码事故的根因，详见 reconcile_stdout 的 docstring）。
所以乱码不是测试失败，而现在也不需要你先 chcp。

注意：`RERANK_ENABLED=true` 时单题可能 60 s 以上（实测 64.7 s/题），
所以默认超时给到 180 s。压测/演示请先把它关掉再跑本脚本。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://localhost:8080"
FALLBACK_KEY = "demo-key-change-me"

# 场景里出现的中文一律写在这里 —— 不经过命令行、不经过 shell 编码，
# 这是脚本存在的核心理由。
ROUTE_CASES: list[tuple[str, str]] = [
    ("VPN 连不上怎么办", "rag"),
    # 工单号必须用语料里真实存在的格式（INC-YYYY-NNNN）。用 TCK-1024 这类
    # 编造格式会让实体抽取失效、退化成关键词查询，而 intent 仍是 tool —— 用例假通过。
    ("查询工单 INC-2026-0412", "tool"),
    ("帮我查工单 INC-2026-0412，顺便说下排查步骤", "hybrid"),
    ("你好", "chitchat"),
]

STREAM_Q = "VPN 连不上怎么办"          # 走 rag，稳定产出 docs + token
TURN1 = "我们生产用的是 MySQL 8.0，我是 DBA"   # 期望抽到 2 条长期事实
TURN2 = "我们的数据库要注意什么"                # 第二轮应带上历史与事实

_results: list[tuple[str, bool, str]] = []

# /ready 报出的运行时 kb_version，给 stream 场景做交叉校验用。
# 单独跑 --only stream 时它是 None，那条交叉断言会自动跳过。
_runtime_kb: int | None = None


# =====================================================================
# 输出编码：Windows 上这是最容易把"通过"看成"乱码"的地方
# =====================================================================
class _Tee:
    """stdout 双写：控制台一份、UTF-8 文件一份。

    为什么不用 PowerShell 的 `Tee-Object`：
      控制台码页是 936（GBK）时，Python 写出的 UTF-8 字节被按 GBK 解码，
      日志里就是 `D1 鑷祴` 这种；而 `Tee-Object` 拷的正是**解码后**的字符串，
      于是坏的那份被原样存进文件 —— 事后连回看都救不回来。
      文件由 Python 自己按 UTF-8 写，就绕开了整个终端编码链。
    """

    def __init__(self, stream: Any, path: Path) -> None:
        self._stream = stream
        self._fh = open(path, "w", encoding="utf-8", errors="replace")

    def write(self, s: str) -> int:
        self._stream.write(s)
        self._fh.write(s)
        return len(s)

    def flush(self) -> None:
        self._stream.flush()
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def console_codepage() -> int | None:
    """当前控制台输出码页（Windows）。非 Windows 或取不到返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def parent_shell() -> str:
    """父进程的可执行文件名（小写）。取不到返回 ""。

    为什么需要它：降级编码这件事**不能只看码页**。
      · PS 5.1（`powershell.exe`）：按 `[Console]::OutputEncoding`（= 码页 936）解原生输出
        → 必须让 Python 写 cp936；
      · PS 7（`pwsh.exe`）：默认按 UTF-8 解 → **必须保持 UTF-8**，降级反而会乱；
      · cmd.exe：字节直接进控制台，按输出码页渲染 → 同样要 cp936。
    只看 `GetConsoleOutputCP()` 会把 pwsh 7 误判成 5.1，于是"修乱码"变成"制造乱码"。
    """
    if sys.platform != "win32":
        return ""
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {os.getppid()}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, errors="replace",
        )
        line = (out.stdout or "").strip().splitlines()
        if not line:
            return ""
        return line[0].split(",")[0].strip().strip('"').lower()
    except Exception:  # noqa: BLE001
        return ""


# 哪些父进程会把"控制台码页"当作解码依据
_ANSI_DECODING_SHELLS = {"powershell.exe", "cmd.exe", "powershell_ise.exe"}


def reconcile_stdout() -> str:
    """把 stdout 的编码对齐到**终端真正用来解码的那个编码**。

    为什么不能只写文档让人去 chcp 65001：
      本机环境有 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，Python 于是写出
      UTF-8 字节；而 PS 5.1（VS Code 集成终端默认就是它）按
      `[Console]::OutputEncoding` = 码页 936 去解码这些字节 —— 屏幕上是
      `D1 鑷祴`，并且 `Tee-Object` 把**解码后**的字符串写进日志，坏的那份
      就永久落盘了。人不可能每次都记得先 chcp。

    注意方向：不是去 `SetConsoleOutputCP(65001)` 让控制台改成 UTF-8 ——
    PS 5.1 的 `[Console]::OutputEncoding` 是启动时抓的快照，改了码页它也不跟着变，
    结果照样乱码。**正确做法是让输出退回到 PS 5.1 期望的编码**（代价是 emoji
    画不出来，见 `_pick_marks()` 的 ASCII 降级）。

    只对"按码页解码"的宿主生效（见 `parent_shell()`），pwsh 7 / bash / 直接重定向
    到文件这些按 UTF-8 解码的场景一律不动。

    **直连真控制台时也绝不能动**（这是踩出来的）：此时 stdout 底层是
    `_WindowsConsoleIO`，Python 用 WriteConsoleW 宽字符直写 —— 中文**本来就正常**。
    而 `sys.stdout.encoding` 在这种路径下也报 'utf-8'，与"管道 + PYTHONIOENCODING=utf-8"
    从字符串上无法区分；若照常 `reconfigure('cp936')`，TextIOWrapper 会编出 GBK 字节，
    而控制台接口仍按 UTF-8 解码这些字节 —— **把本来正常的中文字成乱码**。
    （trace_doctor 在 PS 5.1 直连时乱码，就是这条路径：info 行说"已 re-encoded"，
    标题却是坏的 —— 自愈脚本自己成了乱码的来源。）
    判据是底层 raw 的类型名，不是 encoding 字符串。

    返回一行 ASCII 说明；不需要或无法干预时返回 ""。
    """
    if parent_shell() not in _ANSI_DECODING_SHELLS:
        return ""
    raw = getattr(getattr(sys.stdout, "buffer", None), "raw", None)
    if raw is not None and type(raw).__name__ == "_WindowsConsoleIO":
        # 宽字符直写控制台：中文本来就好，动它反而制造乱码
        return ""
    cp = console_codepage()
    if cp is None or cp == 65001:
        return ""
    enc = (sys.stdout.encoding or "").lower().replace("-", "")
    if enc not in ("utf8", "cp65001"):
        # 本来就按控制台码页输出（环境里没有 PYTHONUTF8 之类），无需干预
        return ""
    if not hasattr(sys.stdout, "reconfigure"):
        return ""
    target = f"cp{cp}"
    try:
        sys.stdout.reconfigure(encoding=target, errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return ""
    # logging 走 stderr：不处理的话，"未配置 key / langfuse 构造超时"这类警告行
    # 在 PS5.1/cmd 管道下照样乱码。独立 try —— stderr 出任何问题都不能影响
    # "stdout 已经修好"这个事实。判定与 stdout 相同：宽字符路径不碰。
    try:
        s_err = sys.stderr
        err_raw = getattr(getattr(s_err, "buffer", None), "raw", None)
        err_enc = (getattr(s_err, "encoding", "") or "").lower().replace("-", "")
        wide = err_raw is not None and type(err_raw).__name__ == "_WindowsConsoleIO"
        if s_err is not None and hasattr(s_err, "reconfigure") and not wide \
                and err_enc in ("utf8", "cp65001"):
            s_err.reconfigure(encoding=target, errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    return (
        f"[info] PS5.1/cmd + codepage {cp}: stdout re-encoded to {target} "
        "(emoji marks degrade to ASCII; chcp 65001 for full UTF-8)"
    )


# 勾叉符号随编码能力降级：GBK 编不出 ✅/✓，硬写就会变成 `?`。
_OK_MARK, _BAD_MARK = "✅", "❌"
_DOT_OK, _DOT_BAD = "✓", "✗"


def _pick_marks() -> None:
    global _OK_MARK, _BAD_MARK, _DOT_OK, _DOT_BAD
    enc = sys.stdout.encoding or "ascii"

    def can(ch: str) -> bool:
        try:
            ch.encode(enc)
            return True
        except Exception:  # noqa: BLE001
            return False

    if not can(_OK_MARK):
        _OK_MARK, _BAD_MARK = "[OK]", "[FAIL]"
    if not can(_DOT_OK):
        _DOT_OK, _DOT_BAD = "[ok]", "[--]"


def console_encoding_warning() -> str:
    """检测"Python 写 UTF-8，但终端按 GBK 解码"这个组合。

    这是**静默错**的：退出码 0、检查项全过，只有眼睛能发现。
    所以宁可多打一行 ASCII 提示，也不要让人拿着一份乱码日志去判断结果。
    """
    codepage = console_codepage()
    if codepage is None or codepage == 65001:
        return ""
    # pwsh 7 / bash 这些按 UTF-8 解码的宿主，UTF-8 输出本来就是对的，
    # 别在这里误报（见 parent_shell() 的说明）。
    if parent_shell() not in _ANSI_DECODING_SHELLS:
        return ""
    enc = (sys.stdout.encoding or "").lower().replace("-", "")
    if enc in ("utf8", "cp65001"):
        return (
            f"[warn] console codepage {codepage}, but python writes UTF-8 -> "
            "Chinese will be mojibake (mojibake != test failure).\n"
            "       fix: run .\\scripts\\selftest.ps1  (or: chcp 65001, or pass --log report.log)"
        )
    return ""


# =====================================================================
# 基础设施
# =====================================================================
def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    mark = _OK_MARK if ok else _BAD_MARK
    print(f"  {mark} {name}" + (f"   [{detail}]" if detail else ""))
    return ok


def load_api_key() -> str:
    """优先环境变量，其次项目 .env，最后回退默认值。

    自动读 .env 是为了让用户不用手抄 key —— 手抄 key 抄错会得到一个
    401，而 401 看起来像"服务坏了"。
    """
    env = os.environ.get("API_KEY")
    if env:
        return env
    path = Path(".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return FALLBACK_KEY


def env_value(name: str, default: str = "") -> str:
    """读一个配置项（环境变量优先，其次 .env）。

    只用于**判断"某个降级是不是预期内的"** —— 例如 `RERANK_ENABLED=false` 时
    链路上必然出现 `degraded(reason="rerank:disabled")`，把它判成失败会让
    "按清单关掉重排"这件事和"链路无降级"自相矛盾。
    """
    if name in os.environ:
        return os.environ[name]
    path = Path(".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def read_stream(
    client: httpx.Client, payload: dict[str, Any], *, timeout: float
) -> list[dict[str, Any]]:
    """POST /chat/stream，把 SSE 帧解析成 dict 列表。

    协议只有 `data: {...}` 与注释帧 `: keepalive`，没有 event: 行
    （见 app/pipeline/orchestrator.py::sse_frame）。

    每个帧额外带一个本地字段 `_t`（相对请求发出时刻的秒数），用来量
    TTFT 与帧间隔 —— **"看起来像不像打字机"不是可用指标**，因为模型吐字快时
    整段答案几百毫秒就推完了。`_t` 只存在于本脚本里，不是协议字段。

    裸 `data: [DONE]` 记成哨兵 `{"type": "_end"}`：它是协议终结符，不是数据帧，
    判断"done 是最后一帧"时必须把它排除，否则永远判失败。
    """
    frames: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    with client.stream("POST", "/chat/stream", json=payload, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            elapsed = round(time.perf_counter() - t0, 3)
            if body == "[DONE]":
                frames.append({"type": "_end", "_t": elapsed})
                continue
            try:
                frame = json.loads(body)
            except json.JSONDecodeError:
                frame = {"type": "_raw", "raw": body}
            if isinstance(frame, dict):
                frame["_t"] = elapsed
            frames.append(frame)
    return frames


def types_of(frames: list[dict[str, Any]]) -> list[str]:
    return [f.get("type", "?") for f in frames]


def first_of(frames: list[dict[str, Any]], type_name: str) -> dict[str, Any]:
    return next((f for f in frames if f.get("type") == type_name), {})


# =====================================================================
# 场景
# =====================================================================
def scene_health(client: httpx.Client, args: argparse.Namespace) -> None:
    """/health 与 /ready：区分"进程活着"和"依赖都在"。"""
    r = client.get("/health")
    check("GET /health 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        body = r.json()
        # 名字是 config_kb_version：它是启动快照，不是运行时值。
        # 曾经叫 kb_version，于是 bump 之后出现"/health=1、meta=2"的自相矛盾。
        check("/health 报出 config_kb_version（启动快照）",
              "config_kb_version" in body,
              f"config_kb_version={body.get('config_kb_version')}")
        check("/health 不冒充运行时版本（字段名不叫 kb_version）",
              "kb_version" not in body,
              "运行时值看 /ready 的 cache 项")

    r = client.get("/ready")
    check("GET /ready 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        return
    body = r.json()
    checks = body.get("checks", {})
    print("    依赖明细：")
    for name, info in checks.items():
        ok = info.get("ok")
        crit = "critical" if info.get("critical") else "可降级"
        print(f"      {_DOT_OK if ok else _DOT_BAD} {name:<18} {crit:<9} {info.get('detail', '')}")
    check("索引产物可用（否则问题全答不了）",
          checks.get("index", {}).get("ok", False),
          checks.get("index", {}).get("detail", ""))
    check("embedding 模型可用", checks.get("embedding_model", {}).get("ok", False))

    # 缓存项的 detail 里带着运行时 kb_version，顶层也放了一份。
    # 这里存下来，给 stream 场景做"meta 与 ready 同源"的交叉校验。
    global _runtime_kb
    cache_info = checks.get("cache", {})
    if isinstance(body.get("kb_version"), int):
        _runtime_kb = body["kb_version"]
    check("缓存探活报出运行时 kb_version（不是配置快照）",
          cache_info.get("ok", False) and isinstance(body.get("kb_version"), int),
          f"kb_version={body.get('kb_version')} · 来源 {body.get('kb_version_source')} · "
          f"{cache_info.get('detail', '')}")
    # 向量库/工具/缓存允许不可用 —— 它们各自有降级路径，所以不判失败，
    # 只在上面明细里显示，避免把"设计上的可降级"读成故障。


def scene_route(client: httpx.Client, args: argparse.Namespace) -> None:
    """/chat/route：不检索、不生成，只看路由决策与工具计划。"""
    for question, expect in ROUTE_CASES:
        r = client.post("/chat/route", json={"message": question})
        if not check(f"/chat/route HTTP 200（{question}）", r.status_code == 200,
                     f"HTTP {r.status_code}"):
            continue
        body = r.json()
        check(f"意图 = {expect}", body.get("intent") == expect,
              f"实际 {body.get('intent')} · {body.get('reason')}")
        check("决策可解释（reason 非空）", bool(body.get("reason")))
        if expect in ("tool", "hybrid"):
            plan = body.get("tool_plan") or []
            check(f"工具计划非空（{expect}）", len(plan) > 0,
                  body.get("tool_plan_summary", ""))
            # 工单号必须**完整**进到工具参数里。之前示例用的是 TCK-1024，
            # 而语料里的工单号形如 INC-2026-0412（见 app/pipeline/intent.py 的 TICKET_NO），
            # 于是实体抽不到、退化成 query_ticket_by_keyword(keyword='tck') ——
            # 计划"非空"、用例照样通过，但真实演示会答非所问。所以这里钉住。
            ticket = "INC-2026-0412"
            if ticket in question:
                check("工具参数带上了完整工单号（不是被截断的关键词）",
                      ticket in json.dumps(plan, ensure_ascii=False),
                      str(plan)[:90])


def scene_stream(client: httpx.Client, args: argparse.Namespace) -> None:
    """整条 SSE 链路的事件协议。

    这里刻意用 `use_cache=False`：缓存命中路径不推 docs / token，
    如果不禁用缓存，第二次运行本脚本就会因为"没有 docs 帧"而假失败。
    """
    frames = read_stream(
        client,
        {"message": STREAM_Q, "use_cache": False},
        timeout=args.timeout,
    )
    ts = types_of(frames)
    check("收到帧", len(ts) > 0, f"{len(ts)} 帧")
    if not ts:
        return

    # `_end` 是本地哨兵（对应裸 `data: [DONE]`），不是数据帧。
    # 之前拿 ts[-1] 直接比 "done"，被自己的哨兵判成失败 —— 典型的假信号。
    body_types = [t for t in ts if t != "_end"]
    check("meta 是第一帧", body_types[0] == "meta", f"实际首帧 {body_types[0]}")
    check("有 route 帧（决策可见）", "route" in body_types)
    check("有 docs 帧（召回结果进了流）", "docs" in body_types, f"帧序 {body_types[:6]}…")
    check("有 token 帧（流式生成）", body_types.count("token") >= 1,
          f"{body_types.count('token')} 帧")
    check("done 是最后一帧数据帧（其后只允许 [DONE] 终结符）",
          body_types[-1] == "done", f"实际末帧 {body_types[-1]}")
    check("流以 [DONE] 终结（客户端不会挂住）", ts[-1] == "_end", f"末帧 {ts[-1]}")

    # 降级：`RERANK_ENABLED=false` 时不再产出 degraded（改成 notice），
    # 所以这里现在期望 **0 条降级帧**；tolerated 只用于兼容旧版本服务端。
    degraded_frames = [f for f in frames if f.get("type") == "degraded"]
    rerank_off = env_value("RERANK_ENABLED", "true").strip().lower() in ("false", "0", "no")
    tolerated = {"rerank:disabled"} if rerank_off else set()
    unexpected = [f for f in degraded_frames if f.get("reason") not in tolerated]
    check(
        "无意外降级（rerank 关闭属配置，不是降级）",
        not unexpected,
        "; ".join(
            f"{f.get('stage')}:{f.get('reason')}" for f in (unexpected or degraded_frames)
        ) or "无降级帧",
    )
    if rerank_off:
        notices = [f for f in frames if f.get("type") == "notice"]
        check(
            "重排关闭仍可见（notice，不伪装成 degraded）",
            any(n.get("code") == "rerank_disabled" for n in notices),
            str(notices),
        )

    docs = first_of(frames, "docs")
    if docs:
        check("docs 里带 sources（可溯源）", bool(docs.get("sources")),
              f"{len(docs.get('sources') or [])} 篇")
        order_by = docs.get("order_by") or "?"
        key_scores = docs.get("rank_scores") or []
        raw_scores = docs.get("scores") or []
        if key_scores:
            check(f"排序键递减（order_by={order_by}）",
                  all(a >= b for a, b in zip(key_scores, key_scores[1:])),
                  f"前 3 = {key_scores[:3]}")
        elif raw_scores:
            print("    （该帧没有 rank_scores：无法判定排序依据，只展示原始分）")
        if raw_scores:
            print(f"    原始分 {raw_scores}  ← 向量 cosine 与 BM25 分**不同量纲**，不能拿来判排序")

    meta = first_of(frames, "meta")
    check("meta 带 kb_version", "kb_version" in meta, f"kb_version={meta.get('kb_version')}")
    if _runtime_kb is not None:
        # meta 的版本号与 /ready 顶层那个必须是同一个值（都来自 Redis）。
        # 曾经 /health 报配置快照、meta 报运行时值，日志里出现 1 与 2 并存，
        # 看的人会以为 bump 没生效 —— 这条断言就是钉住"同源"。
        check("meta.kb_version 与 /ready 同源", meta.get("kb_version") == _runtime_kb,
              f"meta={meta.get('kb_version')} · ready={_runtime_kb}")
    check("meta 标注未命中缓存", meta.get("cache") == "miss", f"cache={meta.get('cache')}")

    done = first_of(frames, "done")
    if done:
        check("done 带 total_ms 与分段耗时",
              "total_ms" in done and "stage_ms" in done,
              f"total_ms={done.get('total_ms')}")

    # 流式的**可测**证据（"看起来像不像打字机"不是证据：模型吐字快时整段会一闪而过）
    token_t = [f.get("_t") for f in frames
               if f.get("type") == "token" and f.get("_t") is not None]
    if token_t:
        gaps = sorted(b - a for a, b in zip(token_t, token_t[1:]))
        median_ms = (gaps[len(gaps) // 2] * 1000) if gaps else 0.0
        print(f"    时间线：TTFT {token_t[0]:.2f}s · token {len(token_t)} 个 · "
              f"帧间隔中位 {median_ms:.1f}ms · 生成段 {token_t[-1] - token_t[0]:.2f}s")


def scene_tools(client: httpx.Client, args: argparse.Namespace) -> None:
    """工具层韧性：skill 命中、来源标注、降级字段可见。

    正常情况下 MCP 健康时应看到 source=mcp；java-mcp 没起时自动降级 mock ——
    两种都算通过（这就是降级的意义），但**必须看得出来自哪**：
    source / breaker / degraded_from 三个字段就是为此存在的。
    """
    frames = read_stream(
        client,
        {"message": "帮我查一下工单 INC-2026-0412 的处理进度。", "use_cache": False},
        timeout=args.timeout,
    )
    calls = [f for f in frames if f.get("type") == "tool_call"]
    results = [f for f in frames if f.get("type") == "tool_result"]
    check("有工具调用帧", len(calls) >= 1, str([f.get("name") for f in calls]))
    check("有工具结果帧", len(results) >= 1, str([f.get("tool") for f in results]))
    if not calls or not results:
        return

    skill_calls = [f for f in calls if f.get("skill")]
    check("skill 命中（tool_call 带 skill 字段，业务意图可见）",
          bool(skill_calls),
          str(sorted({f.get("skill") for f in skill_calls})))

    r0 = results[0]
    check("结果带 source（数据来源如实标注）",
          r0.get("source") in ("mcp", "mock", "cache"), str(r0.get("source")))
    check("结果带 attempts（重试了几次要能读出来）",
          isinstance(r0.get("attempts"), int) and r0.get("attempts") >= 1,
          str(r0.get("attempts")))
    degraded_from = r0.get("degraded_from")
    # 判据用 breaker 而不是"工具名里有没有 mcp"：后者恒为假（工具名是业务名），
    # 会让这条断言永远不执行、永远"通过" —— 静默失效比断言失败危险得多。
    # breaker 只有装配了 MCP provider 才会被填（tools/__init__.py），所以
    # "breaker 非空 + source=mock" 正好等价于"配了 MCP 但降级了"。
    if r0.get("source") == "mock" and r0.get("breaker"):
        check("从 mcp 降级到 mock 时带 degraded_from（假数据不能装成真数据）",
              isinstance(degraded_from, list) and "mcp" in degraded_from,
              str(degraded_from))
    if r0.get("breaker"):
        print(f"    熔断器状态：{r0.get('breaker')}（open = 本次回答发生在熔断期，数据来自 mock）")


def scene_memory(client: httpx.Client, args: argparse.Namespace) -> None:
    """三层记忆：第二轮必须带上第一轮的历史与事实。

    两个 session id 都必须新生成 —— 复用旧 id 会让"第一轮无 memory"
    这条断言失效（旧会话已经有历史了）。
    """
    sid = f"selftest-{uuid.uuid4().hex[:8]}"
    print(f"    会话 {sid}")

    r1 = read_stream(client, {"message": TURN1, "session_id": sid, "use_cache": False},
                     timeout=args.timeout)
    meta1 = first_of(r1, "meta")
    # 字段**在**但计数为 0，这才是实现的实际语义：带着 session_id 就一定查了一次 store，
    # "查过且为空"比"没有字段"更有信息量（后者分不清是没查还是没数据）。
    mem1 = meta1.get("memory")
    check("第一轮历史与事实计数为 0（首次对话的正常行为）",
          isinstance(mem1, dict) and mem1.get("history_turns") == 0 and mem1.get("facts") == 0,
          f"实际 {mem1}")

    r2 = read_stream(client, {"message": TURN2, "session_id": sid, "use_cache": False},
                     timeout=args.timeout)
    meta2 = first_of(r2, "meta")
    mem = meta2.get("memory")
    check("第二轮 meta 带 memory 统计", isinstance(mem, dict), f"实际 {mem}")
    if isinstance(mem, dict):
        check("history_turns ≥ 1（会话记忆生效）", mem.get("history_turns", 0) >= 1,
              f"history_turns={mem.get('history_turns')}")
        check("facts ≥ 1（长期事实抽取生效）", mem.get("facts", 0) >= 1,
              f"facts={mem.get('facts')}")

    degraded = [f for f in r2 if f.get("type") == "degraded"]
    check("第二轮无 memory 降级", not any(f.get("stage") == "memory" for f in degraded),
          str([f.get("reason") for f in degraded]) if degraded else "")

    # 顺带验证"记忆进了 prompt"而不是只存不用：
    # 第二轮回答里出现 MySQL 这类背景词，说明历史/事实真的被注入了。
    # token 帧的字段是 `content`（不是 text）。早先这里写的是 `text`，
    # 取到空串后 `if answer:` 直接跳过断言 —— 一条"永远通过"的假用例。
    answer = "".join(f.get("content", "") for f in r2 if f.get("type") == "token")
    check("第二轮真的有正文（否则下面那条断言无从谈起）", bool(answer),
          f"{len(answer)} 字")
    if answer:
        hit = "mysql" in answer.lower()
        check("回答引用了第一轮背景（MySQL）", hit,
              "规则版事实抽取 + 滑窗注入的联合效果；不命中时先看 meta.memory.facts")


def scene_isolation(client: httpx.Client, args: argparse.Namespace) -> None:
    """换一个 session 问同一句话，不能带出别人的历史 —— 多副本下的硬要求。"""
    fresh = f"selftest-{uuid.uuid4().hex[:8]}"
    frames = read_stream(client, {"message": TURN2, "session_id": fresh, "use_cache": False},
                        timeout=args.timeout)
    meta = first_of(frames, "meta")
    mem_fresh = meta.get("memory")
    check("新会话的历史与事实计数为 0（跨会话隔离）",
          isinstance(mem_fresh, dict)
          and mem_fresh.get("history_turns") == 0 and mem_fresh.get("facts") == 0,
          f"实际 {mem_fresh}")

    # 非法 session_id 必须被当"无会话"处理，而不是拼进 Redis 键里。
    bad = read_stream(client, {"message": TURN2, "session_id": "bad id*with:符号",
                               "use_cache": False}, timeout=args.timeout)
    meta_bad = first_of(bad, "meta")
    check("非法 session_id 被静默忽略（不报错、不进记忆）",
          "memory" not in meta_bad,
          "白名单 ^[A-Za-z0-9_-]{1,64}$；非法 id 按无会话处理")


def scene_cache(client: httpx.Client, args: argparse.Namespace) -> None:
    """两级缓存：同一问题问两遍，第二遍必须命中。

    不关心第一遍是 miss 还是已经命中（脚本可以重复跑），只要求
    "第二遍一定命中" —— 这样这条用例是幂等的。
    """
    question = "VPN 连不上怎么办"
    first = read_stream(client, {"message": question, "use_cache": True}, timeout=args.timeout)
    second = read_stream(client, {"message": question, "use_cache": True}, timeout=args.timeout)

    c1 = first_of(first, "meta").get("cache")
    c2 = first_of(second, "meta").get("cache")
    check("第二遍命中缓存（exact/semantic）", c2 in ("exact", "semantic"),
          f"第一遍={c1} 第二遍={c2}")
    check("命中时不再检索（无 docs 帧）", "docs" not in types_of(second),
          "缓存命中该直接短路；出现 docs 说明缓存没生效")
    sim = first_of(second, "meta").get("similarity")
    if sim is not None:
        check("命中时给出相似度（可解释）", 0 < sim <= 1, f"similarity={sim}")


def _trace_dirs() -> list[Path]:
    """trace 可能落在哪。

    容器里写 `/app/traces`（compose 的 bind mount），宿主上对应
    `data/traces` —— 两个路径不同名，所以都试一遍，而不是要求使用者
    先记住映射关系。这正是"自测脚本要能一条命令跑通"的一部分。
    """
    repo = Path(__file__).resolve().parents[1]
    candidates: list[Path] = []
    env = os.environ.get("OBSERVABILITY_TRACE_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(repo / "data" / "traces")
    return candidates


def _load_recent_traces(max_files: int = 3) -> list[dict[str, Any]]:
    from app.observability.tracing import iter_trace_records

    rows: list[dict[str, Any]] = []
    for directory in _trace_dirs():
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl"))[-max_files:]:
            rows.extend(iter_trace_records(path))
    return rows


def _wait_for_trace(trace_id: str, *, deadline: float = 4.0) -> list[dict[str, Any]]:
    """轮询等落盘。写盘是同步 flush 的，正常应立刻可见；
    留这个循环是为了多副本 + 网络文件系统下不至于因为一次抢先读而判失败。"""
    end = time.time() + deadline
    while time.time() < end:
        rows = [r for r in _load_recent_traces() if r.get("trace_id") == trace_id]
        if rows:
            return rows
        time.sleep(0.2)
    return []


def scene_trace(client: httpx.Client, args: argparse.Namespace) -> None:
    """全链路追踪：埋点真的落盘了、阶段齐全、TTFT 可查、能按会话回放。

    这一场景验的是"可观测"本身，而不是业务链路 —— 所以它必须能在
    **没有 Langfuse key** 的情况下通过（本地 JSONL 是全量事实源）。
    Langfuse 是否上报，由 OBSERVABILITY/LANGFUSE 配置决定，不在断言范围内。
    """
    sid = f"selftest-trace-{uuid.uuid4().hex[:8]}"
    frames = read_stream(
        client,
        {"message": "如何排查 502 与 504", "session_id": sid, "use_cache": False},
        timeout=args.timeout,
    )
    ts = types_of(frames)
    check("请求正常收尾（埋点没有改变协议）", bool(ts) and ts[-1] == "_end",
          f"实际末帧 {ts[-1] if ts else '无'}")

    tid = (first_of(frames, "meta") or {}).get("trace_id", "")
    check("meta 帧带 trace_id（凭它就能回放）", bool(tid), str(first_of(frames, "meta")))

    rows = _wait_for_trace(tid) if tid else []
    if not rows:
        check("trace 已落盘", False,
              f"在 {[str(d) for d in _trace_dirs()]} 里找不到 trace_id={tid or '-'}；"
              "确认 OBSERVABILITY_ENABLED 未关闭、/app/traces 挂载可写")
        return

    stages = [r.get("stage") for r in rows]
    check("trace 已落盘", True, f"{len(rows)} 个 span")
    for stage in ("route", "embed", "cache_lookup", "recall", "generate"):
        check(f"埋到 {stage}", stage in stages, f"stages={stages}")

    turn = next((r for r in rows if r.get("stage") == "turn"), {})
    meta = turn.get("metadata") or {}
    ttft = meta.get("ttft_ms")
    check("turn 收尾并记了 TTFT", isinstance(ttft, (int, float)) and ttft > 0, str(ttft))
    check("TTFT 早于整轮结束（不是把 total 当 TTFT）",
          bool(ttft) and ttft <= (turn.get("duration_ms") or 0) + 1,
          f"ttft={ttft} total={turn.get('duration_ms')}")

    gen = next((r for r in rows if r.get("stage") == "generate"), {})
    gmeta = gen.get("metadata") or {}
    check("生成段记了首字位置（相对 span 起点）",
          isinstance(gmeta.get("first_token_offset_ms"), (int, float)),
          str(gmeta.get("first_token_offset_ms")))
    check("生成段记了模型名（成本/diff 都要用）", bool(gmeta.get("model")),
          str(gmeta.get("model")))

    check("每行都带同一个 session_id（会话可串起来）",
          all(r.get("session_id") == sid for r in rows),
          f"{sorted({r.get('session_id') for r in rows})}")
    check("turn 记了路由与缓存口径（回放可解释）",
          bool(meta.get("route")) and bool(meta.get("cache")),
          f"route={meta.get('route')} cache={meta.get('cache')}")

    from scripts.trace_replay import group_by_trace

    groups = group_by_trace(rows)
    check("回放能按 trace 聚合", len(groups) == 1, f"{len(groups)} 组")
    missing = [r.get("stage") for r in rows if not isinstance(r.get("start_ms"), (int, float))]
    check("回放的时间线可排序（start_ms 齐全）", not missing, f"缺 start_ms：{missing}")


def scene_memory_down(client: httpx.Client, args: argparse.Namespace) -> None:
    """记忆层失效必须可见，且不能拖垮回答。

    运行前请自己停掉 redis：`docker stop ops-redis`。
    """
    sid = f"selftest-{uuid.uuid4().hex[:8]}"
    frames = read_stream(client, {"message": TURN2, "session_id": sid, "use_cache": False},
                        timeout=args.timeout)
    ts = types_of(frames)
    degraded = [f for f in frames if f.get("type") == "degraded"]
    memory_deg = [f for f in degraded if f.get("stage") == "memory"]

    check("出现 stage=memory 降级事件", bool(memory_deg),
          "没有降级事件说明记忆层其实没挂，或降级逻辑没生效")
    check("降级后回答仍然完成", ts and ts[-1] == "_end",
          f"实际末帧 {ts[-1] if ts else '无'}")
    check("降级有可读说明", bool(memory_deg and memory_deg[0].get("message")),
          memory_deg[0].get("message", "") if memory_deg else "")


SCENES = {
    "health": scene_health,
    "route": scene_route,
    "stream": scene_stream,
    "tools": scene_tools,
    "memory": scene_memory,
    "isolation": scene_isolation,
    "cache": scene_cache,
    "trace": scene_trace,
    "memory_down": scene_memory_down,
}
DEFAULT_SCENES = ["health", "route", "stream", "tools", "memory", "isolation", "cache", "trace"]


# =====================================================================
# 入口
# =====================================================================
def _finish(code: int) -> int:
    """收尾：把 --log 文件刷盘后再退出。

    不 close 的话块缓冲会丢掉最后几行，而最后几行恰好是结论
    （"结果：46/46 通过"）—— 最不该丢的一行。
    """
    out = sys.stdout
    try:
        out.flush()
    except Exception:  # noqa: BLE001
        pass
    if isinstance(out, _Tee):
        out.close()
        sys.stdout = out._stream
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="D1 接口级自测（LangGraph + 三层记忆）")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--api-key", default=None, help="默认读环境变量 API_KEY 或项目 .env")
    parser.add_argument("--only", default="", help=f"逗号分隔，可选：{', '.join(SCENES)}")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="单次流式请求超时；RERANK_ENABLED=true 时单题可能 60s+")
    parser.add_argument("--list", action="store_true", help="列出场景后退出")
    parser.add_argument("--use-proxy", action="store_true",
                        help="尊重 HTTP_PROXY 环境变量（默认直连，避免本机请求被代理吞成 502）")
    parser.add_argument("--log", default=None, metavar="PATH",
                        help="同时把报告按 UTF-8 写一份到文件（绕开终端编码，PS 下看日志用这个）")
    args = parser.parse_args()

    # 编码对齐要放在**任何中文输出之前**：一行都不许先写出去。
    # 顺序是 先对齐 stdout → 再按新编码挑勾叉符号 → 再挂 --log 双写。
    note = reconcile_stdout()
    _pick_marks()
    if note:
        print(note)
    else:
        warn = console_encoding_warning()
        if warn:
            print(warn)

    if args.log:
        log_path = Path(args.log).expanduser()
        if log_path.parent and not log_path.parent.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = _Tee(sys.stdout, log_path)
        print(f"报告同时写入：{log_path.resolve()}（UTF-8）")

    if args.list:
        print("可用场景：")
        for name, fn in SCENES.items():
            print(f"  {name:<12} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return _finish(0)

    names = [n.strip() for n in args.only.split(",") if n.strip()] or DEFAULT_SCENES
    unknown = [n for n in names if n not in SCENES]
    if unknown:
        print(f"未知场景 {unknown}；可选：{', '.join(SCENES)}")
        return _finish(2)

    key = args.api_key or load_api_key()
    base = args.base_url.rstrip("/")
    print("=" * 68)
    print(f"D1 自测 · {base} · 场景 {', '.join(names)}")
    print("=" * 68)

    t0 = time.perf_counter()
    with httpx.Client(base_url=base, headers={"X-API-Key": key}, timeout=args.timeout,
                      trust_env=args.use_proxy) as client:
        # 先确认服务在，否则后面每个场景都会各自报连接错误，噪音很大
        try:
            client.get("/health")
        except httpx.HTTPError as exc:
            print(f"\n连不上 {base}：{type(exc).__name__}: {exc}")
            print("先起服务：.\\scripts\\dev.ps1 ps   →   没起就 .\\scripts\\dev.ps1 build")
            print("首次启动要加载模型，等 healthcheck 变 healthy（start_period 90s）再跑。")
            print("若是 502 且端口上没人监听：本机 HTTP_PROXY 把请求转走了，"
                  "取消代理或加 NO_PROXY=localhost,127.0.0.1。")
            return _finish(1)

        for name in names:
            print(f"\n[{name}] {(SCENES[name].__doc__ or '').strip().splitlines()[0]}")
            try:
                SCENES[name](client, args)
            except httpx.HTTPStatusError as exc:
                check(f"{name} 请求成功", False,
                      f"HTTP {exc.response.status_code}: {exc.response.text[:120]}")
            except httpx.TimeoutException:
                check(f"{name} 未超时", False,
                      f"超过 {args.timeout}s —— 重排开着时单题可达 60s+，调大 --timeout 或关重排")
            except Exception as exc:  # noqa: BLE001
                check(f"{name} 未抛异常", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [(n, d) for n, ok, d in _results if not ok]
    cost = time.perf_counter() - t0

    print("\n" + "=" * 68)
    print(f"结果：{passed}/{len(_results)} 通过 · 耗时 {cost:.1f}s")
    if failed:
        print("\n失败项：")
        for name, detail in failed:
            print(f"  {_BAD_MARK} {name}" + (f"   [{detail}]" if detail else ""))
        print("\n排查顺序：① 先看 .\\scripts\\dev.ps1 logs 里的启动日志 "
              "② 再看 /ready 的依赖明细 ③ 最后才怀疑代码")
    else:
        print("全部通过 —— 本阶段（编排 + 三层记忆）接口层无问题。")
    print("=" * 68)
    return _finish(0 if not failed else 1)


if __name__ == "__main__":
    sys.exit(main())
