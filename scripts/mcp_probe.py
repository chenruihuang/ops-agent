"""Java MCP Server 协议与故障注入自检（一条命令替代手敲 curl）。

为什么要有这个脚本
------------------
在 Windows PowerShell 5.1 里手敲嵌套引号的 JSON body 会静默失败：

    docker exec ... sh -c 'curl ... -d "{\\"jsonrpc\\":\\"2.0\\",...}"'

PowerShell 把整段 `sh -c '...'` 当**一个**参数传给 docker.exe 时，会用双引号包起来
却不转义内层的双引号 —— 内层引号把参数劈开，curl 根本没收到 body。
服务端只看到一句 `Required request body is missing`，返回 400，
看起来像"Java 服务坏了"，实际是命令行被截断（实测：同一命令在 bash 里 200，在 PS 5.1 里 400）。

本脚本用 subprocess 的 **argv 列表** 调 docker，body 走 stdin（`--data-binary @-`），
全程没有 shell、没有引号拼接；同时用 utf-8 显式解码，避开控制台 cp936 造成的乱码。

用法
----
    python -m scripts.mcp_probe                 # 用默认容器 ops-agent-java-mcp-1
    python -m scripts.mcp_probe --url http://localhost:8081/mcp   # 端口映射到宿主时
    python -m scripts.mcp_probe --no-chaos      # 跳过故障注入（生产环境别打）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from typing import Any

DEFAULT_CONTAINER = "ops-agent-java-mcp-1"
INNER_URL = "http://127.0.0.1:8080/mcp"
INNER_HEALTH = "http://127.0.0.1:8080/health"

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


# ---------------------------------------------------------------- 传输层


def _argv(rest: list[str], container: str | None) -> list[str]:
    if container:
        return ["docker", "exec", "-i", container, *rest]
    return rest


def _run(rest: list[str], container: str | None, stdin: bytes | None,
         timeout: float) -> tuple[int, bytes]:
    """返回 (curl 退出码, stdout 字节)。stdout 末行是我们加的 HTTP 状态码。"""
    cmd = _argv(rest, container)
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 28, b"<timeout>"
    return proc.returncode, proc.stdout


def call(method: str, params: dict[str, Any] | None, *,
         container: str | None, url: str, headers: dict[str, str] | None = None,
         timeout: float = 30.0) -> tuple[int, dict[str, Any] | None, float]:
    """发一次 JSON-RPC 调用。返回 (http_status, 解析后的响应或 None, 服务端耗时ms)。

    耗时**必须**取 curl 的 `%{time_total}`，不能用 Python 侧的 perf_counter：
    在 Docker Desktop 上 `docker exec` 自身每跳就要 1~3.4 s（实测），
    拿墙钟减出来的是「docker 启动开销 + HTTP」，会把 sleep=400ms 的注入
    淹没在噪声里 —— 于是「注入没生效」和「注入生效了但被 exec 吞掉」看起来一样。
    """
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    rest = ["curl", "-s", "-w", "\n%{http_code} %{time_total}", "-X", "POST", url,
            "-H", "Content-Type: application/json"]
    for key, value in (headers or {}).items():
        rest += ["-H", f"{key}: {value}"]
    rest += ["--data-binary", "@-"]

    rc, raw = _run(rest, container, body, timeout)
    if rc == 28:
        return 0, None, float(timeout * 1000)

    text = raw.decode("utf-8", "replace")
    head, _, tail = text.rpartition("\n")
    status = 0
    elapsed_ms = 0.0
    parts = tail.split()
    try:
        status = int(parts[0])
        elapsed_ms = float(parts[1]) * 1000
    except (IndexError, ValueError):
        return 0, None, 0.0
    try:
        parsed = json.loads(head) if head.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed, elapsed_ms


def get(url: str, *, container: str | None, timeout: float = 15.0) -> tuple[int, str]:
    rc, raw = _run(["curl", "-s", "-w", "\n%{http_code}", url], container, None, timeout)
    if rc != 0:
        return 0, ""
    text = raw.decode("utf-8", "replace")
    head, _, tail = text.rpartition("\n")
    try:
        return int(tail.strip()), head
    except ValueError:
        return 0, text


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Java MCP 协议与 chaos 自检")
    parser.add_argument("--container", default=None,
                        help="容器名；不指定时用 %s；给了 --url/--health 则默认直连宿主"
                             "（显式传空串 '' 也等于直连宿主）" % DEFAULT_CONTAINER)
    parser.add_argument("--url", default=None, help="覆盖 MCP 地址（默认容器内 %s）" % INNER_URL)
    parser.add_argument("--health", default=None, help="覆盖 health 地址")
    parser.add_argument("--no-chaos", action="store_true", help="跳过 X-Chaos 故障注入用例")
    parser.add_argument("--slow-ms", type=int, default=400, help="sleep 注入的毫秒数")
    args = parser.parse_args(argv)

    # 传输通道必须与 URL 匹配，否则"探测不到"和"服务挂了"长得一模一样：
    # 给了宿主地址却还在容器里 curl，容器内没有那个端口 → 连接被拒 → 全部 status=0。
    # 这是本脚本要防的那类假阴性，所以判据写在这里而不是靠使用者记得加 --container ""。
    if args.container is not None:
        container = args.container or None          # 显式空串 = 直连宿主
    elif args.url or args.health:
        container = None                            # 给了宿主地址就别套 docker exec
    else:
        container = DEFAULT_CONTAINER
    url = args.url or INNER_URL
    health = args.health or (url.rsplit("/mcp", 1)[0] + "/health")

    where = f"container={container}" if container else "direct"
    print(f"目标：{url}（{where}）\n")

    # 1) health
    status, body = get(health, container=container)
    check("GET /health 返回 200", status == 200, f"status={status}")
    check("/health 体是 JSON 且不含 error", '"error"' not in body, body[:80])
    # **200 不等于打对了服务**：FastAPI 也有 /health，也返回 200。
    # 唯一的区别在响应体 —— java-mcp 是 {"status":"ok","tools":N}，
    # API 是 {"status":"ok","model":...,"prompt_version":...}。
    # 所以必须按 body 形状断言，否则打错端口时前面的检查会全绿。
    is_mcp_health = False
    try:
        is_mcp_health = isinstance(json.loads(body).get("tools"), int)
    except (json.JSONDecodeError, AttributeError):
        is_mcp_health = False
    check("/health 带 tools 字段（确认对端是 java-mcp 而非 API）", is_mcp_health, body[:80])
    if status == 200 and not is_mcp_health:
        print("  提示：这个 /health 是 Python API 的形状（含 model/prompt_version），"
              "说明该端口不是 java-mcp。\n"
              "        宿主 8080 是 nginx → FastAPI；java-mcp 的宿主端口见 "
              "docker-compose.yml 的 ports（当前 18081）。")

    # 2) initialize
    status, resp, _ = call("initialize", {"protocolVersion": "2025-06-18",
                                          "clientInfo": {"name": "mcp_probe"}},
                           container=container, url=url)
    result = (resp or {}).get("result") or {}
    check("initialize 返回 200", status == 200, f"status={status}")
    check("initialize 带 serverInfo.name", bool(result.get("serverInfo", {}).get("name")),
          json.dumps(result, ensure_ascii=False)[:120])

    # 3) tools/list —— 与 Python 侧 TOOL_SPECS 对齐（单一来源，不写死名单）
    status, resp, _ = call("tools/list", None, container=container, url=url)
    tools = ((resp or {}).get("result") or {}).get("tools") or []
    remote = {t.get("name") for t in tools}
    check("tools/list 返回 200", status == 200, f"status={status}")
    try:
        sys.path.insert(0, ".")
        from app.tools import TOOL_SPECS  # noqa: PLC0415

        expected = {spec.name for spec in TOOL_SPECS}
        missing = expected - remote
        extra = remote - expected
        check(f"工具集合与 TOOL_SPECS 一致（{len(expected)} 个）", not missing and not extra,
              f"缺={sorted(missing)} 多={sorted(extra)}")
    except Exception as exc:  # noqa: BLE001 - 单跑脚本时应能容忍导入失败
        check("工具数量 = 5", len(tools) == 5, f"实际 {len(tools)}，且导入 TOOL_SPECS 失败：{exc}")
    for tool in tools:
        schema = tool.get("inputSchema") or {}
        check(f"{tool.get('name')} 有 required 字段", bool(schema.get("required")),
              json.dumps(schema, ensure_ascii=False)[:100])

    # 4) tools/call 正常路
    status, resp, ms = call("tools/call", {"name": "query_ticket_by_no",
                                           "arguments": {"ticket_no": "INC-2026-0412"}},
                            container=container, url=url)
    result = (resp or {}).get("result") or {}
    content = result.get("content") or []
    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    check("tools/call 返回 200", status == 200, f"status={status}")
    check("返回内容里带工单号", "INC-2026-0412" in text, text[:100] or "<空>")
    check("返回 structuredContent", bool(result.get("structuredContent")),
          json.dumps(result, ensure_ascii=False)[:100])
    print(f"         （单次调用 {ms:.0f} ms）")

    # 5) 未知工具 → JSON-RPC 业务错误，而不是 500 崩溃
    status, resp, _ = call("tools/call", {"name": "no_such_tool", "arguments": {}},
                            container=container, url=url)
    err = (resp or {}).get("error") or {}
    check("未知工具返回 JSON-RPC error 且带 code", bool(err.get("code")),
          f"status={status} body={json.dumps(resp, ensure_ascii=False)[:100]}")

    if not args.no_chaos:
        print("\n故障注入（X-Chaos）：")
        # 6) error=500,times=2 → 前两次失败，第三次恢复
        #
        # 尾部的 nonce 不是装饰：ChaosFilter 用 **header 原串** 做计数器 key，
        # 且 10 分钟窗口内不重置。同一个字面量连跑两次，第一次就把 times 用完了，
        # 第二次会看到 [200,200,200] —— 看起来像"注入坏了"，其实是计数没归零。
        # 附加一个解析器会忽略的键，等于每组用例拿一个全新计数槽。
        nonce = uuid.uuid4().hex[:8]
        chaos = f"error=500,times=2,nonce={nonce}"
        codes = []
        for _ in range(3):
            status, _, _ = call("tools/call", {"name": "query_ticket_by_no",
                                               "arguments": {"ticket_no": "INC-2026-0412"}},
                                container=container, url=url,
                                headers={"X-Chaos": chaos})
            codes.append(status)
        check("error=500,times=2 → [500, 500, 200]（计数器按次递减）",
              codes == [500, 500, 200], f"实际 {codes}（chaos={chaos}）")

        # 7) sleep 注入 → 耗时下限可观测
        status, _, ms = call("tools/call", {"name": "query_ticket_by_no",
                                            "arguments": {"ticket_no": "INC-2026-0412"}},
                             container=container, url=url,
                             headers={"X-Chaos": f"sleep={args.slow_ms}"})
        check(f"sleep={args.slow_ms} 注入生效（耗时 ≥ {args.slow_ms}ms）",
              status == 200 and ms >= args.slow_ms * 0.9, f"status={status} {ms:.0f}ms")

        # 8) 注入结束、不带 X-Chaos 时立即恢复（chaos 是请求级，不该留状态）
        status, _, ms = call("tools/call", {"name": "query_ticket_by_no",
                                            "arguments": {"ticket_no": "INC-2026-0412"}},
                             container=container, url=url)
        check("撤掉 X-Chaos 后恢复（200 且不慢）", status == 200 and ms < args.slow_ms * 0.9,
              f"status={status} {ms:.0f}ms")
    print("\n提示：chaos 计数在容器内按时间窗重置；异常残留时 `docker compose restart java-mcp`。")

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 条通过" + (f"，{FAIL} 条失败" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
