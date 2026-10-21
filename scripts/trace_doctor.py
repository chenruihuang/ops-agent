"""追踪自检：配了 key 却看不到数据时，先跑这个。

为什么需要一台"医生"
--------------------
Langfuse 接入的失败方式几乎全是**静默**的：

  · key 没配 → 只落本地，不上报（代码刻意不报错，否则连本地都看不到）
  · key 配错 → SDK 在后台重试，请求照样 200，UI 上什么都没有
  · host 写错 / 网络不通 → 同上，且日志只有一行 "Transient error"
  · 采样率写成 0.01 → 100 次请求只上去 1 条，看起来像"大部分丢了"
  · 容器里 trace 目录不可写 → 本地也没了，回放直接空

这些现象的对外表现完全一样："没有数据"。靠猜要花很久，所以把判据固化：

    python -m scripts.trace_doctor              # 全量检查
    python -m scripts.trace_doctor --send-test  # 额外真的发一个 span 到 Langfuse

最后一项是**唯一能证明"端到端通了"的动作**：`auth_check()` 只验认证，
不验 trace 能不能落地（配额、project 配置、网络白名单都可能拦在写入这一层）。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings          # noqa: E402
from app.observability.tracing import (        # noqa: E402
    Tracer,
    build_tracer,
    iter_trace_records,
)
from scripts.selftest import reconcile_stdout  # noqa: E402

OK = "  [OK]  "
BAD = "  [!!]  "
WARN = "  [--]  "


def line(mark: str, name: str, detail: str = "") -> None:
    print(f"{mark}{name}" + (f"   {detail}" if detail else ""))


def check_config() -> None:
    print("\n[1] 配置")
    line(OK, "OBSERVABILITY_ENABLED", str(settings.observability_enabled))
    line(OK, "OBSERVABILITY_TRACE_DIR", str(settings.observability_trace_dir))
    line(OK, "OBSERVABILITY_SAMPLE_RATE",
         f"{settings.observability_sample_rate}（只影响云端上报，本地永远全量）")

    has_keys = bool(settings.langfuse_public_key.strip()
                    and settings.langfuse_secret_key.strip())
    if not settings.langfuse_enabled:
        line(WARN, "LANGFUSE_ENABLED=false", "本地追踪可用，不上报云端")
    elif not has_keys:
        line(WARN, "Langfuse 未配置 key",
             "只落本地 JSONL；要上报请填 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
    else:
        pk = settings.langfuse_public_key
        sk = settings.langfuse_secret_key
        line(OK, "Langfuse key", f"public={pk[:6]}…{pk[-4:]} secret=***{sk[-4:]}")
        line(OK, "Langfuse host", settings.langfuse_host)
        if settings.langfuse_host.rstrip("/") == "https://cloud.langfuse.com" \
                and pk.startswith("pk-lf-") is False:
            line(WARN, "public key 前缀不是 pk-lf-", "自托管与 Cloud 的 key 不通用，确认一下")


def check_dir() -> Path | None:
    print("\n[2] 本地落盘")
    directory = Path(settings.observability_trace_dir or "data/traces")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        line(BAD, "目录创建失败", f"{directory}：{exc}")
        return None
    # 探针用**固定文件名 + 覆盖写**，不删除。
    # 原因是实测踩过：某些 Windows 环境下 `unlink()` 会走回收站接口并失败
    # （`SHFileOperationW 失败: 0x2`），于是"能写"被误判成"不可写" ——
    # 一个纯粹由清理动作制造出来的假故障。判据只该关心"写不写得进去"。
    probe = directory / ".writable"
    try:
        probe.write_text(str(time.time()), encoding="utf-8")
        line(OK, "目录可写", str(directory.resolve()))
    except Exception as exc:  # noqa: BLE001
        line(BAD, "目录不可写", f"{exc}（容器内请确认 /app/traces 有 bind mount）")
        return None

    files = sorted(directory.glob("*.jsonl"))
    if not files:
        line(WARN, "还没有 trace 文件", "先发一次请求：.\\scripts\\dev.ps1 ask \"如何排查 502\"")
        return directory
    latest = max(files, key=lambda p: p.stat().st_mtime)
    rows = list(iter_trace_records(latest))
    age = time.time() - latest.stat().st_mtime
    line(OK, "最近文件", f"{latest.name} · {len(rows)} 条 · {age / 60:.1f} 分钟前")
    turns = [r for r in rows if r.get("stage") == "turn"]
    if turns:
        tail = turns[-1]
        meta = tail.get("metadata") or {}
        line(OK, "最近一轮",
             f"total {tail.get('duration_ms')}ms · TTFT {meta.get('ttft_ms')}ms"
             f" · route={meta.get('route')} · cache={meta.get('cache')}")
        if meta.get("ttft_ms") is None:
            line(WARN, "这一轮没有 TTFT", "确认 request 是否走到了 generate/cache 终局")
    return directory


def check_langfuse(send_test: bool) -> None:
    print("\n[3] Langfuse 连通")
    from app.observability.tracing import _host_reachable

    reachable, why = _host_reachable(settings.langfuse_host)
    if not reachable:
        line(BAD, "host 不可达", f"{why} —— 检查网络/代理/端口，或改用自托管地址")
        return
    line(OK, "host 可达", settings.langfuse_host)

    t0 = time.perf_counter()
    tracer = build_tracer(settings=settings)
    init_ms = (time.perf_counter() - t0) * 1000
    backend = tracer.langfuse
    if backend is None:
        line(WARN, "未启用上报", "见第 1 节；本地 JSONL 不受影响")
        return
    # 这个数字值得打出来：SDK 5.0.1 在宿主上构造要 10~19 秒（5.0.2 容器里 0.6 秒）。
    # 不打出来的话，用户会以为脚本卡死了。
    line(OK, "客户端已构造", f"{init_ms:.0f}ms（只在启动时付一次）")

    ok, detail = backend.auth_check()
    if ok:
        line(OK, "auth_check 通过", "key 有效")
    else:
        # auth_check 失败时 SDK 会把响应头一起抛出来，太长，这里只留有用部分
        brief = detail[:160].replace("\n", " ")
        line(BAD, "auth_check 失败", brief)
        if "401" in detail:
            line(WARN, "401 = key 与 host 不匹配",
                 "Cloud 的 key 只能配 cloud.langfuse.com；自托管要用它自己生成的 key")
        elif "timed out" in detail.lower():
            line(WARN, "超时", "网络到不了 API；自托管检查端口与反向代理")
        line(WARN, "认证失败也能验证链路", "说明请求确实打到了 Langfuse 服务端，只差有效 key")

    if not send_test:
        line(WARN, "未做端到端验证", "加 --send-test 真的发一个 span（证明写入链路通，不只认证）")
        tracer.close()
        return

    turn = tracer.start_turn(message="trace_doctor 自检", session_id="trace-doctor")
    with turn.stage("doctor", input={"probe": True}) as span:  # type: ignore[union-attr]
        span.set(output={"ok": True})
    turn.finish(answer="trace_doctor ok", route="doctor", cache="none")  # type: ignore[union-attr]
    t1 = time.perf_counter()
    tracer.flush()
    line(OK, "已发送并 flush", f"{(time.perf_counter() - t1) * 1000:.0f}ms")
    trace_id = getattr(turn, "trace_id", "")
    line(WARN, "去 UI 核对",
         f"{settings.langfuse_host.rstrip('/')}/trace/{trace_id}（几秒内出现；"
         "若一直没有，多半是 key 无效 —— flush 是异步重试的，不会在请求里报错）")
    tracer.close()


def main(argv: list[str] | None = None) -> int:
    note = reconcile_stdout()
    if note:
        print(note)
    parser = argparse.ArgumentParser(prog="trace_doctor", description="追踪链路自检")
    parser.add_argument("--send-test", action="store_true",
                        help="真的发一个测试 trace 到 Langfuse（验证写入链路，不只是认证）")
    args = parser.parse_args(argv)

    print("=" * 68)
    print("追踪自检 · 本地 JSONL 为主，Langfuse 为可选上报")
    print("=" * 68)
    check_config()
    check_dir()
    check_langfuse(args.send_test)
    print("\n结论：")
    print("  · 本地 JSONL 通 = 可以回放：python -m scripts.trace_replay --session <id>")
    print("  · Langfuse 通 = 云端有 Sessions/Traces 视图，能按会话串起多轮")
    print("  · 两者互不依赖：云端挂了不影响本地，本地关掉也不影响云端")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
