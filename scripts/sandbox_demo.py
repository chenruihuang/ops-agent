"""交互式体验安全沙箱：挂起 → 人工确认 → 执行/拒绝 → 审计。

为什么单独一个脚本，而不是用 `stream_chat` + 手敲 curl：

1. `stream_chat` 把 `token` 打成一行提示就结束了，token 要肉眼复制到另一条
   curl 命令里 —— 演示时手一抖就复制错，还容易把 token 打进聊天记录；
2. 沙箱的价值在**时序**：先看到"被拦住了、要你决策"，再看"决策之后发生了什么"。
   两条独立命令之间隔着几十秒，观众感知不到这是一条流程；
3. 这个脚本会把"挂起原因 / 待批参数 / 风险等级 / 有效期"排成一张审批单，
   再把人做的每一次决定连同回执一起打出来 —— 和真实工单系统的审批页同构。

用法（在 D:\\ai-project\\ops-agent 下）：

    py -3.12 -m scripts.sandbox_demo                    # 交互模式，一行一个问题
    py -3.12 -m scripts.sandbox_demo "帮我重启 order-service"
    py -3.12 -m scripts.sandbox_demo --auto             # 无人值守跑完四幕（录屏用）

交互模式里除了直接问问题，还认这几个命令：

    /audit    看最近 8 条审计记录（挂起 / 批准 / 拒绝 / 策略拒绝）
    /help     看可用示例问法
    /quit     退出

退出码：0 正常；1 连接失败 / 401。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

# 四幕演示：策略拒绝 → 挂起确认 → 挂起拒绝 → 凭据重放。
# 顺序有讲究：先给一个"不用审批也能走通"的对照（策略拒绝那题会正常作答），
# 观众才不会以为"凡是写操作都只会被拦"。
SCENARIOS: tuple[tuple[str, str], ...] = (
    ("帮我重启 payment-gateway", "策略先于审批：这个动作本身被禁，连审批单都不生成"),
    ("帮我重启 order-service", "挂起待确认：参数快照 + 一次性凭据，批准后执行"),
    ("把 order-service 扩容到 5 个副本", "挂起后拒绝：凭据立即作废，不会执行"),
    ("帮我重启 notify-service", "一次性：批准后再拿同一 token 请求，必须 404"),
)

C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_CYAN = "\033[36m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_OFF = "\033[0m"


def _color(text: str, code: str) -> str:
    """Windows 10+ 的 conhost / Windows Terminal 支持 ANSI；不支持时也只是多点字符，
    比引一个 colorama 依赖划算。"""
    return f"{code}{text}{C_OFF}"


def _env_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _rule(char: str = "-", width: int = 72) -> None:
    print(_color(char * width, C_DIM))


def _disp_width(text: str) -> int:
    """终端显示宽度：中文/全角算 2 列。

    直接用 len() 会让审批单的右边框逐行右移（"动作" 两字在终端占 4 列），
    调试时会被当成排版 bug 查半天，实际只是宽度算错。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


@dataclass
class Turn:
    """一轮对话的观测结果。只留做判断要用的字段。"""

    frames: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    route: str = "?"
    cache: str = "?"
    chars: int = 0
    awaiting: bool = False
    error: str = ""


def run_turn(
    client: httpx.Client,
    message: str,
    *,
    session_id: str | None,
    timeout: float,
) -> Turn:
    """跑一轮 /chat/stream，边收边打，返回结构化观测结果。"""
    payload: dict = {"message": message, "use_cache": False}
    if session_id:
        payload["session_id"] = session_id

    turn = Turn()
    t0 = time.perf_counter()
    ttft_ms: float | None = None

    print()
    _rule("=")
    print(_color(f"[提问] {message}", C_BOLD))
    _rule("=")

    try:
        with client.stream("POST", "/chat/stream", json=payload, timeout=timeout) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")
                turn.error = f"HTTP {resp.status_code}: {body[:200]}"
                print(_color(f"[失败] {turn.error}", C_RED))
                return turn

            for line in resp.iter_lines():
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    frame = json.loads(body)
                except json.JSONDecodeError:
                    continue

                turn.frames.append(frame)
                kind = frame.get("type")

                if kind == "token":
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    piece = frame.get("content") or ""
                    turn.chars += len(piece)
                    sys.stdout.write(piece)
                    sys.stdout.flush()
                elif kind == "meta":
                    turn.cache = frame.get("cache", "?")
                    turn.route = frame.get("route", "?")
                elif kind == "route":
                    signals = frame.get("signals") or []
                    extra = f" needs_write" if frame.get("needs_write") else ""
                    print(_color(
                        f"[路由] intent={frame.get('intent')}{extra} "
                        f"reason={frame.get('reason')} signals={signals}", C_DIM))
                elif kind == "tool_result":
                    src = frame.get("source")
                    status = frame.get("status") or ""
                    # 挂起 / 拒绝**不是失败**：ok=False 只是"没执行"。
                    # 直接打 FAIL 会让人以为工具坏了，这正是沙箱最容易被误解的地方。
                    if status == "pending_confirmation":
                        print(_color(
                            f"[工具] {frame.get('tool')} 已拦截 · 等待人工确认", C_YELLOW))
                    elif status == "denied":
                        print(_color(
                            f"[工具] {frame.get('tool')} 被策略拒绝："
                            f"{frame.get('error') or ''}", C_RED))
                    else:
                        mark = "OK" if frame.get("ok") else "FAIL"
                        print(_color(
                            f"[工具] {frame.get('tool')} {mark} source={src} "
                            f"{frame.get('elapsed_ms')}ms {frame.get('records')} 条", C_DIM))
                elif kind == "degraded":
                    print(_color(f"[降级] {json.dumps(frame, ensure_ascii=False)}", C_YELLOW))
                elif kind == "notice":
                    print(_color(f"[提示] {frame.get('code')} {frame.get('message', '')}", C_DIM))
                elif kind == "confirm_required":
                    turn.pending.append(frame)
                elif kind == "done":
                    turn.awaiting = frame.get("status") == "awaiting_confirmation"
                    total = (time.perf_counter() - t0) * 1000
                    if turn.awaiting:
                        # 两种挂起形态要分开说：纯写操作（没有正文可答）
                        # 与 hybrid（知识已答、写操作另等确认）。混成一句
                        # "没有生成回答"会让用户以为答案丢了。
                        answered = (
                            f"已生成 {turn.chars} 字（知识部分已答）" if turn.chars
                            else "没有生成回答"
                        )
                        print(_color(
                            f"\n[本轮结束] {answered} —— 另有 {len(turn.pending)} 个写操作挂起，"
                            f"等你决策（route={turn.route} 耗时 {total:.0f}ms）", C_YELLOW))
                    else:
                        ttft = f"{ttft_ms:.0f}ms" if ttft_ms is not None else "—"
                        print(_color(
                            f"\n[本轮结束] route={turn.route} cache={turn.cache} "
                            f"字符={turn.chars} TTFT={ttft} 耗时 {total:.0f}ms", C_DIM))
    except httpx.HTTPError as exc:
        turn.error = f"{type(exc).__name__}: {exc}"
        print(_color(f"\n[失败] {turn.error}", C_RED))
    return turn


def show_approval(frame: dict) -> None:
    """把一帧 confirm_required 渲染成审批单。

    字段顺序刻意是"人做决定时真正要看的顺序"：
    要动什么 → 动哪个对象 → 为什么 → 有什么风险 → 凭据多久过期。
    """
    risk = str(frame.get("risk") or "")
    label = {"dangerous": "危险", "write": "写操作"}.get(risk, risk or "未知")
    color = C_RED if risk == "dangerous" else C_YELLOW
    inner = 72
    bar = "─" * inner

    def line(text: str) -> None:
        # 超宽就折行，不截断 —— 审批单上少一个字都可能是致命的
        while _disp_width(text) > inner:
            cut, acc = 0, 0
            for i, ch in enumerate(text):
                acc += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                if acc > inner:
                    break
                cut = i + 1
            # 别把 JSON 从中间劈开（"notify-servi / ce" 很难读）：
            # 在断点前 40% 的范围内回退到最近的分隔符
            for j in range(cut - 1, max(int(cut * 0.6), 0), -1):
                if text[j] in ' ,，、":）)':
                    cut = j + 1
                    break
            print(_color("│" + _pad(text[:cut], inner) + "│", color))
            text = "  " + text[cut:].lstrip()
        print(_color("│" + _pad(text, inner) + "│", color))

    print()
    print(_color("┌" + bar + "┐", color))
    line("  待确认动作（挂起中，尚未执行）")
    print(_color("├" + bar + "┤", color))
    rows = [
        ("动作", str(frame.get("tool"))),
        ("参数", json.dumps(frame.get("args"), ensure_ascii=False)),
        ("风险", label),
        ("原因", str(frame.get("reason") or "—")),
        ("执行", "模拟执行（不会真的调用编排接口）" if frame.get("simulated") else "真实执行"),
        ("有效期", f"{frame.get('expires_in')} 秒，且只能用一次"),
        ("凭据", f"{str(frame.get('token'))[:12]}...（不进正文、不进缓存）"),
    ]
    for key, value in rows:
        line(_pad(f"  {key}", 10) + value)
    print(_color("└" + bar + "┘", color))
    print(_color("  注：上面那轮 SSE 已经结束（done.status=awaiting_confirmation）。", C_DIM))
    print(_color("      审批是另一次请求 POST /chat/confirm，凭据存在 Redis 里等 10 分钟 ——", C_DIM))
    print(_color("      所以多副本、进程重启都不丢；反过来，同一 token 也用不了第二次。", C_DIM))


def show_receipt(data: dict, token: str, *, replay: bool = False) -> None:
    status = data.get("status")
    ok = bool(data.get("ok"))
    title = "回执" if not replay else "回执（重放）"
    color = C_GREEN if ok else C_RED
    print()
    print(_color(f"[{title}] status={status} ok={ok}", color))
    if "tool" in data:
        print(f"  工具：{data.get('tool')}  参数：{json.dumps(data.get('args'), ensure_ascii=False)}")
    if "source" in data:
        sim = "（模拟执行）" if data.get("simulated") else ""
        print(f"  来源：{data.get('source')}{sim}  用时：{data.get('elapsed_ms')}ms")
    if data.get("message"):
        print(_color(f"  说明：{data['message']}", C_YELLOW))
    text = (data.get("text") or "").strip()
    if text:
        print("  输出：")
        for line in text.splitlines():
            print(f"    {line}")
    if data.get("error"):
        print(_color(f"  错误：{data['error']}", C_RED))


def confirm(client: httpx.Client, frame: dict, session_id: str | None) -> None:
    """对一条挂起动作做决定。y=批准 n=拒绝 v=看原始帧 r=批准并演示重放。"""
    token = str(frame.get("token") or "")
    body: dict = {"token": token}
    if session_id:
        body["session_id"] = session_id

    while True:
        try:
            choice = input(_color(
                "\n你的决定 [y=批准执行 / n=拒绝 / r=批准并验证一次性 / v=看原始帧 / s=跳过] > ",
                C_CYAN)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "v":
            print(json.dumps(frame, ensure_ascii=False, indent=2))
            continue
        if choice in ("s", ""):
            print(_color("[跳过] 凭据会留到过期（10 分钟）后自动失效，不算执行过", C_DIM))
            return
        if choice not in ("y", "n", "r"):
            print(_color("请输入 y / n / r / v / s", C_YELLOW))
            continue

        approve = choice in ("y", "r")
        payload = dict(body, approve=approve)
        try:
            resp = client.post("/chat/confirm", json=payload)
        except httpx.HTTPError as exc:
            print(_color(f"[失败] 确认请求发不出去：{exc}", C_RED))
            return

        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "status": f"http_{resp.status_code}", "message": resp.text[:200]}

        if resp.status_code == 404:
            print(_color(f"[404] {data.get('status')}: {data.get('message')}", C_RED))
        elif resp.status_code == 403:
            print(_color("[403] 这个 token 不属于当前会话（越权尝试被拒）", C_RED))
        else:
            show_receipt(data, token)

        if choice == "r" and resp.status_code == 200:
            # 同一 token 再来一次：这是"一次性"最直观的证据
            print(_color("\n[验证一次性] 再发一次同一个 token ...", C_DIM))
            try:
                again = client.post("/chat/confirm", json=payload)
                print(_color(
                    f"  第二次返回 HTTP {again.status_code} → "
                    f"{json.dumps(again.json(), ensure_ascii=False)}", C_GREEN))
            except httpx.HTTPError as exc:
                print(_color(f"  第二次请求失败：{exc}", C_RED))
        return


def show_audit(client: httpx.Client) -> None:
    try:
        resp = client.get("/admin/sandbox/audit", params={"limit": 8})
    except httpx.HTTPError as exc:
        print(_color(f"[失败] {exc}", C_RED))
        return
    if resp.status_code != 200:
        print(_color(f"[失败] HTTP {resp.status_code}: {resp.text[:200]}", C_RED))
        return
    data = resp.json()
    print()
    _rule("=")
    print(f"审计日志  store={data.get('store')}  enabled={data.get('enabled')}  "
          f"writable={data.get('writable')}")
    if not data.get("writable"):
        print(_color(f"  注意：审计文件不可写（{data.get('last_error')}）", C_RED))
    _rule("=")
    for rec in data.get("records") or []:
        ts = time.strftime("%H:%M:%S", time.localtime(rec.get("ts", 0)))
        event = rec.get("event", "?")
        color = {"pending": C_YELLOW, "approved": C_GREEN, "rejected": C_RED,
                 "denied": C_RED, "invalid_grant": C_RED}.get(event, C_DIM)
        args = rec.get("args") or {}
        detail = json.dumps(args, ensure_ascii=False) if args else ""
        print(_color(f"  {ts}  {event:<14}{rec.get('tool') or '':<20}{detail}", color))
        if rec.get("reason"):
            print(_color(f"          reason: {rec['reason']}", C_DIM))
    print(_color("  完整记录：data/audit/sandbox.jsonl", C_DIM))


HELP = """可用示例问法：

  帮我重启 order-service              挂起 → 批准后执行（模拟）
  帮我重启 payment-gateway            策略直接拒绝（这个动作被禁）
  把 order-service 扩容到 5 个副本     写操作，挂起后可以选 n 拒绝
  order-service 现在有没有告警          普通只读查询，对照用（不挂起）
  重启 order-service 的步骤是什么       知识题，走 RAG，不该触发工具

  /audit  看审计   /help 看这个   /quit 退出"""


def repl(client: httpx.Client, session_id: str, timeout: float) -> int:
    print(_color("安全沙箱交互体验 · 输入问题回车发送，/help 看示例，/quit 退出", C_BOLD))
    while True:
        try:
            line = input(_color("\n问题 > ", C_CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        low = line.lower()
        if low in ("/quit", "/exit", "/q"):
            return 0
        if low in ("/help", "/h", "?"):
            print(HELP)
            continue
        if low == "/audit":
            show_audit(client)
            continue

        turn = run_turn(client, line, session_id=session_id, timeout=timeout)
        if turn.error:
            return 1
        if turn.pending:
            show_approval(turn.pending[0])
            confirm(client, turn.pending[0], session_id)
        elif turn.awaiting:
            print(_color("[注意] 本轮标记为挂起，但没有收到待确认帧 —— 检查事件契约", C_RED))


def auto(client: httpx.Client, session_id: str, timeout: float) -> int:
    """无人值守跑完四幕。第二幕自动批准，第三幕自动拒绝，第四幕自动重放。"""
    print(_color("自动演示模式：四幕连跑，不需要手动输入", C_BOLD))
    failures = 0

    for idx, (question, note) in enumerate(SCENARIOS, start=1):
        print()
        print(_color(f"### 第 {idx} 幕 · {note}", C_BOLD))
        turn = run_turn(client, question, session_id=session_id, timeout=timeout)
        if turn.error:
            failures += 1
            continue

        if not turn.pending:
            if idx == 1:
                # 第一幕走这条：策略拒绝在规划阶段就发生了，本轮照常有答案
                denied = [f for f in turn.frames
                          if f.get("type") == "tool_result" and f.get("status") == "denied"]
                print(_color(
                    f"[检查] 没有审批单（符合预期）；policy denied 帧 {len(denied)} 个；"
                    f"正文 {turn.chars} 字", C_DIM))
                if not denied:
                    failures += 1
                    print(_color("[检查失败] 预期被策略拒绝，但没看到 denied 帧", C_RED))
            else:
                failures += 1
                print(_color(f"[检查失败] 第 {idx} 幕预期挂起，实际没有", C_RED))
            continue

        show_approval(turn.pending[0])
        approve = idx in (2, 4)
        if idx == 3:
            print(_color("\n[自动] 第三幕：选择拒绝（n）", C_DIM))
        else:
            print(_color(f"\n[自动] 第 {idx} 幕：选择批准（y）", C_DIM))

        token = str(turn.pending[0].get("token"))
        payload = {"token": token, "approve": approve}
        if session_id:
            payload["session_id"] = session_id
        resp = client.post("/chat/confirm", json=payload)
        data = resp.json() if resp.content else {}
        show_receipt(data, token)

        want = "rejected" if idx == 3 else "executed"
        if data.get("status") != want:
            failures += 1
            print(_color(f"[检查失败] 预期 status={want}，实际 {data.get('status')}", C_RED))
        if idx in (2, 4) and not data.get("simulated"):
            failures += 1
            print(_color("[检查失败] 回执没有标 simulated=true —— "
                         "模拟执行必须能被看出来", C_RED))

        if idx == 4:
            print(_color("\n[自动] 同一 token 再请求一次（验证一次性）...", C_DIM))
            again = client.post("/chat/confirm", json=payload)
            print(_color(f"  返回 HTTP {again.status_code} → "
                         f"{json.dumps(again.json(), ensure_ascii=False)}", C_GREEN))
            if again.status_code != 404:
                failures += 1
                print(_color("  预期 404，实际不是 —— 凭据没有做到一次性", C_RED))

    print()
    show_audit(client)
    print()
    print(_color(f"四幕结束，失败检查 {failures} 项", C_GREEN if failures == 0 else C_RED))
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="交互式体验安全沙箱：挂起 → 确认 → 执行/拒绝 → 审计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP,
    )
    parser.add_argument("message", nargs="*", help="问题文本；不给则进入交互模式")
    parser.add_argument("-s", "--session", default="sbx-demo", help="session_id（默认 sbx-demo）")
    parser.add_argument("--auto", action="store_true", help="无人值守跑完四幕演示")
    parser.add_argument("--api", default=os.getenv("OPS_API", "http://127.0.0.1:8080"),
                        help="服务地址")
    parser.add_argument("--key", default=None, help="X-API-Key；缺省读 .env 的 API_KEY")
    parser.add_argument("--timeout", type=float, default=300.0, help="读超时秒数")
    args = parser.parse_args(argv)

    key = args.key or _env_key()
    if not key:
        print("[失败] 没找到 API_KEY：用 --key 传，或在 .env 里配 API_KEY=", file=sys.stderr)
        return 1

    session_id = args.session or None
    print(f"[连接] {args.api}  session_id={session_id}")

    with httpx.Client(
        base_url=args.api,
        headers={"X-API-Key": key},
        timeout=args.timeout,
        trust_env=False,          # 宿主 HTTP_PROXY 会劫持 127.0.0.1
    ) as client:
        if args.auto:
            return auto(client, session_id, args.timeout)
        if args.message:
            turn = run_turn(client, " ".join(args.message).strip(),
                            session_id=session_id, timeout=args.timeout)
            if turn.error:
                return 1
            if turn.pending:
                show_approval(turn.pending[0])
                confirm(client, turn.pending[0], session_id)
            return 0
        return repl(client, session_id, args.timeout)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        # 管道输入（echo/printf 喂脚本）是 UTF-8 字节流，而 Windows 默认按 cp936
        # 解码 stdin，中文问题会直接抛 UnicodeDecodeError。只在非 tty 时改 ——
        # 交互终端里 input() 走的是控制台 Unicode 通道，改了反而可能出问题。
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 非 TTY 环境没有 reconfigure
        pass
    raise SystemExit(main())
