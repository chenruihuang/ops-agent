"""手工看一条流式回答（带 session_id）。

为什么要一个脚本而不是 curl：

1. PowerShell 5.1 里拼 `-d "{\"message\":\"...\"}"` 会把中文和内层引号弄坏，
   请求体丢失时服务端只回 400，看起来像接口坏了；
2. curl 的输出是原始 SSE 行，token 帧要肉眼拼；这里直接把 token 连起来打，
   把 meta / route / tool_call / degraded / done 压成一行摘要；
3. `--session` 两轮就能验证会话记忆（第二轮 meta.memory.history_turns 应 > 0）。

用法（在 D:\\ai-project\\ops-agent 下）：

    python -m scripts.stream_chat "连接池耗尽的处置步骤是什么" --session demo-01
    python -m scripts.stream_chat "那 inode 耗尽呢" --session demo-01
    python -m scripts.stream_chat "INC-2026-0412 处理到哪了" --session demo-02 --no-cache
    python -m scripts.stream_chat "问题" --session demo-03 --show-frames
    python -m scripts.stream_chat "问题" --api http://127.0.0.1:8080 --key sk-xxx

退出码：0 正常；1 请求失败 / 401 / 连接不上。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

# 摘要里只打这几个字段，避免一屏都是 kb_version/trace_id
META_KEYS = ("cache", "route", "similarity", "kb_version", "memory")
ROUTE_KEYS = ("intent", "confidence", "reason", "signals")


def _env_key() -> str:
    """从 .env 读 API_KEY；不引入 dotenv，少一个依赖。"""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _pick(frame: dict, keys: tuple[str, ...]) -> dict:
    return {k: frame[k] for k in keys if k in frame and frame[k] not in (None, "")}


def stream_once(
    client: httpx.Client,
    message: str,
    *,
    session_id: str | None,
    use_cache: bool,
    show_frames: bool,
) -> int:
    payload: dict = {"message": message, "use_cache": use_cache}
    if session_id:
        payload["session_id"] = session_id

    t0 = time.perf_counter()
    ttft_ms: float | None = None
    chars = 0
    cache = route = "?"

    print(f"\n[请求] {message}")
    print(f"[参数] session_id={session_id or '(无)'}  use_cache={use_cache}")
    try:
        with client.stream("POST", "/chat/stream", json=payload) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")
                print(f"[失败] HTTP {resp.status_code}: {body[:300]}", file=sys.stderr)
                return 1

            for line in resp.iter_lines():
                if not line or line.startswith(":"):
                    continue                      # 空行 / ": keepalive"
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    frame = json.loads(body)
                except json.JSONDecodeError:
                    print(f"[非 JSON] {body[:120]}")
                    continue

                kind = frame.get("type")
                if show_frames:
                    print(f"\n[frame {time.perf_counter() - t0:6.2f}s] {json.dumps(frame, ensure_ascii=False)}")

                if kind == "token":
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    piece = frame.get("content") or ""
                    chars += len(piece)
                    sys.stdout.write(piece)       # 不换行、不缓冲 —— 这才是"流式"的观感
                    sys.stdout.flush()
                elif kind == "meta":
                    cache = frame.get("cache", "?")
                    route = frame.get("route", "?")
                    print(f"[meta] {json.dumps(_pick(frame, META_KEYS), ensure_ascii=False)}")
                elif kind == "route":
                    print(f"[route] {json.dumps(_pick(frame, ROUTE_KEYS), ensure_ascii=False)}")
                elif kind in ("tool_call", "tool_result", "docs"):
                    print(f"[{kind}] {json.dumps(frame, ensure_ascii=False)[:220]}")
                elif kind in ("degraded", "notice", "error"):
                    print(f"[{kind}] {json.dumps(frame, ensure_ascii=False)}")
                elif kind == "confirm_required":
                    # 写操作被沙箱挂起。这一帧是**唯一**能拿到 token 的地方 ——
                    # 不打印出来，演示就没法继续（token 不会出现在正文里，
                    # 也不会进缓存：它是动作级凭据，不是内容）。
                    token = frame.get("token", "")
                    print(f"[confirm_required] {frame.get('risk')} · "
                          f"{frame.get('tool')}({json.dumps(frame.get('args'), ensure_ascii=False)})")
                    print(f"                  原因：{frame.get('reason')}")
                    if frame.get("simulated"):
                        print("                  注意：这是模拟执行，不会真的动线上服务")
                    print(f"                  token={token}（{frame.get('expires_in')}s 内有效，只能用一次）")
                    print("                  确认：curl -s --noproxy '*' -X POST "
                          f"{str(client.base_url).rstrip('/')}/chat/confirm "
                          "-H \"X-API-Key: $OPS_API_KEY\" -H 'Content-Type: application/json' "
                          f"-d '{{\"token\":\"{token}\",\"approve\":true}}'")
                    print("                  拒绝：把 approve 改成 false（凭据同时作废）")
                elif kind == "done":
                    total = (time.perf_counter() - t0) * 1000
                    ttft = f"{ttft_ms:.0f}ms" if ttft_ms is not None else "—"
                    if frame.get("status") == "awaiting_confirmation":
                        # 两种挂起形态：纯写操作没有正文；hybrid 的正文已经流式打完。
                        # 都写成"没有生成回答"会让用户以为答案丢了 —— 这跟大盘把
                        # "查询失败"渲染成"无流量"是同一类错误：状态被合并了。
                        body = (f"已生成 {chars} 字" if chars
                                else "本轮没有生成回答")
                        print(
                            f"\n[待确认] {body}（挂起 {len(frame.get('pending') or [])} 个写操作）"
                            f" route={route} 总耗时={total:.0f}ms"
                        )
                    else:
                        print(
                            f"\n[完成] cache={cache} route={route} 字符={chars} "
                            f"TTFT={ttft} 总耗时={total:.0f}ms"
                        )
    except httpx.HTTPError as exc:
        print(f"\n[失败] 连接错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="调用 /chat/stream 并把流式回答直接打到终端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("message", nargs="+", help="问题文本（多个词会自动拼成一句）")
    parser.add_argument("-s", "--session", default="demo", help="session_id（默认 demo，同 id 串上下文）")
    parser.add_argument("--no-cache", action="store_true", help="use_cache=false，强制走完整链路")
    parser.add_argument("--api", default=os.getenv("OPS_API", "http://127.0.0.1:8080"), help="服务地址")
    parser.add_argument("--key", default=None, help="X-API-Key；缺省读 .env 的 API_KEY")
    parser.add_argument("--show-frames", action="store_true", help="打印每个原始 SSE 帧（排障用）")
    parser.add_argument("--timeout", type=float, default=300.0, help="读超时秒数")
    args = parser.parse_args(argv)

    key = args.key or _env_key()
    if not key:
        print("[失败] 没找到 API_KEY：用 --key 传，或在 .env 里配 API_KEY=", file=sys.stderr)
        return 1

    message = " ".join(args.message).strip()
    print(f"[连接] {args.api}  (Ctrl+C 中断)")

    # trust_env=False：宿主 HTTP_PROXY 会劫持 127.0.0.1（实测踩过）
    with httpx.Client(
        base_url=args.api,
        headers={"X-API-Key": key},
        timeout=args.timeout,
        trust_env=False,
    ) as client:
        return stream_once(
            client,
            message,
            session_id=args.session or None,
            use_cache=not args.no_cache,
            show_frames=args.show_frames,
        )


if __name__ == "__main__":
    # Windows 控制台可能是 GBK，中文 token 会直接抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 非 TTY 环境没有 reconfigure
        pass
    raise SystemExit(main())
