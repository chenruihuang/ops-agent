"""舰队截图场景 —— 与 data/source/fleet.py 同源派生的 84 张"截图"。

设计同 scripts/gen_screenshots.py：
  1. 内容与文本语料呼应：每个舰队服务两张图 —— 首要故障模式的终端日志
     + 资源水位监控面板（kind="panel"）。
  2. 数字与语料**共用同一套派生公式**（限流值 / 阈值 / 内存 limit 都从
     服务行号算出来），保证"截图里的数字 = 文档里的数字 = 评估锚点"。
  3. related 只指向舰队自己生成的 doc_id + 已确认存在的全局文档。

用法：不单独运行。由 gen_screenshots.py 在模块底部 SCENES.extend(extra_scenes())。
gen_image_transcripts.py 导入 SCENES 时会一并带出本模块的场景（转写自动覆盖）。
"""

from __future__ import annotations

from data.source.fleet import (
    _FLEET_FAILMODES,
    _FLEET_TABLE,
    fm_symptom_short,
)

RED, YELLOW, GREEN = "#ff5f56", "#f5c542", "#4ade80"
TODAY = "2026-10-23"


def _svc_row(i: int) -> tuple:
    return _FLEET_TABLE[i]


def _first_failmode(i: int) -> str:
    return _FLEET_FAILMODES[i % 8][0]


def _terminal_lines(i: int) -> list[tuple[str, str]]:
    """按故障模式生成终端日志行（数字与语料同源）。"""
    row = _svc_row(i)
    key, name = row[0], row[1]
    code = row[7]
    fm = _first_failmode(i)
    v = i
    is_java = "Java" in row[3]
    pod = f"{key}-7d9f8b6c4-x2k9n"
    lines: list[tuple[str, str]] = [("cmd", f"$ kubectl logs -f {pod} -n app --tail=20")]

    if fm == "oom":
        mem = 2 + (v % 4)
        lines += [
            ("ts", f"{TODAY} 10:{(v % 50) + 10:02d}:07.331 INFO  [http-nio] {key} ok path=/api/v1 latency=42ms"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:19.880 WARN  [mem-watch] RSS {mem * 1024 - 120}MiB / limit {mem}Gi (99.0%)，连续 3 个采样点上涨"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:21.104 ERROR  fatal error: out of memory" if not is_java
             else f"{TODAY} 10:{(v % 50) + 10:02d}:21.104 ERROR  java.lang.OutOfMemoryError: Java heap space"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:21.662 ERROR  container will be OOMKilled (exit 137)"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:22.010 WARN   k8s restartCount=4 (limit 5), BackOff 3m12s"),
            ("cmd", "$ kubectl top pod " + pod + " --containers"),
            ("ok",  "POD                             CONTAINER  CPU(cores)  MEMORY(bytes)"),
            ("ok",  f"{pod}   app        1421m       {mem * 1024 - 87}Mi (limit {mem}Gi)"),
            ("hl",  f"# 判读：内存贴 limit {mem}Gi + OOMKill -> 处置见 rb-{key}-oom；错误码 {code}-503-001 伴随出现"),
        ]
        return lines

    if fm == "upstream-timeout":
        t = 1 + (v % 3)
        lines += [
            ("ts", f"{TODAY} 10:{(v % 50) + 10:02d}:31.220 INFO  [http-nio] {key} ok path=/api/v1 latency=61ms"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:33.901 ERROR  upstream connect timeout after {t}s (upstream P99 {2000 + v * 100}ms)"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:34.118 ERROR  respond 503 {code}-503-001 trace_id=7f3a{v:02d}c9"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:35.450 WARN  5xx ratio last 1m = {2 + v % 4}% (baseline 0.1%)"),
            ("cmd", "$ kubectl logs --tail=200 -l app=" + key + " | grep -c Timeout"),
            ("ok",  f"{140 + v * 7}"),
            ("hl",  f"# 判读：{code}-503-001 集中 -> 依赖超时 -> 处置见 rb-{key}-upstream-timeout；先限流止血"),
        ]
        return lines

    if fm == "conn-pool":
        pool = 20 + (v % 3) * 10
        lines += [
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:02.550 WARN  HikariPool-1 - Connection is not available, request timed out after 30000ms"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:02.880 WARN  pool stats: active={pool}/{pool}, idle=0, waiting=23"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:03.004 ERROR  respond 503 {code}-503-001 pool exhausted"),
            ("cmd", "$ mysql -e \"show processlist\" | awk '{print $5}' | sort | uniq -c"),
            ("ok",  f"  18 Query   12 Sleep  {3 + v % 5} Sending data"),
            ("hl",  f"# 判读：active={pool}/{pool} 打满 + 大量 Running -> 处置见 rb-{key}-conn-pool；只 kill 查询不 kill 连接"),
        ]
        return lines

    if fm == "mq-backlog":
        lag = (v + 1) * 5000
        lines += [
            ("ts", f"{TODAY} 10:{(v % 50) + 10:02d}:00.100 INFO  consumer {key}-group partition={v % 12} offset advanced 1"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:15.400 WARN  consumer lag={lag} (threshold {lag // 2}), growth +{180 + v % 40}/s"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:15.401 WARN  single-message cost p99={900 + v * 30}ms (baseline 40ms)"),
            ("cmd", "$ kafka-consumer-groups --describe --group " + key + "-group"),
            ("ok",  f"TOPIC            PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG"),
            ("ok",  f"{key}-topic   {v % 12}          {lag * 3}         {lag * 4}         {lag}"),
            ("hl",  f"# 判读：lag 突破 {lag} 且单条耗时上涨 -> 处置见 rb-{key}-mq-backlog；先看下游再扩消费者"),
        ]
        return lines

    if fm == "cache-penetration":
        hit = 60 + v % 15
        lines += [
            ("ts", f"{TODAY} 10:{(v % 50) + 10:02d}:09.001 INFO  cache stats hit_ratio={hit}% (baseline 99%)"),
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:09.120 WARN  db qps {(v + 1) * 300} (baseline {(v + 1) * 30}), cpu 93%"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:09.310 ERROR  respond 500 {code}-500-001 downstream db slow"),
            ("cmd", f"$ redis-cli --scan --pattern '{key}:*' | head -3"),
            ("ok",  f"{key}:not-exist-{v}01   {key}:not-exist-{v}02   {key}:not-exist-{v}03"),
            ("hl",  f"# 判读：命中率 {hit}% + 空回源为主 -> 缓存穿透 -> 处置见 rb-{key}-cache-penetration；先上空值缓存"),
        ]
        return lines

    if fm == "threadpool":
        core = 50 + (v % 3) * 50
        lines += [
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:41.700 WARN  thread-pool active={core * 2}/{core * 2}, queue={core * 10}/{core * 10}"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:41.810 ERROR  rejected: queue full, respond 503 {code}-503-002"),
            ("cmd", "$ kubectl exec " + pod + " -- " + ("jstack 1 | grep -c WAITING" if is_java else "sh -c 'kill -QUIT 1' 2>/dev/null || pprof goroutine")),
            ("ok",  f"{core * 2 + 11} threads waiting on upstream io"),
            ("hl",  f"# 判读：队列打满 + CPU 不高 -> 线程被下游同步等待占住 -> 处置见 rb-{key}-threadpool"),
        ]
        return lines

    if fm == "slow-query":
        lines += [
            ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:51.100 WARN  db cpu 93%, slow_query count last 5m = {40 + v}"),
            ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:51.220 ERROR  sql took {2 + v % 3}.4s rows=18233 table={key}_main"),
            ("cmd", "$ pt-query-digest slow.log --limit 3"),
            ("ok",  f"Rank  Query ID            Response time  Calls"),
            ("ok",  f"1     0x{v:02d}F3A9C1  {40 + v}.20  {30 + v}     SELECT {key}_main"),
            ("hl",  f"# 判读：慢查询集中在新 SQL -> 处置见 rb-{key}-slow-query；EXPLAIN 确认索引，变更走 pol-db-change"),
        ]
        return lines

    # disk-full
    pct = 85 + v % 5
    return [
        ("cmd", f"$ kubectl exec {pod} -- df -h /data/logs"),
        ("ok",  "Filesystem      Size  Used Avail Use% Mounted on"),
        ("warn", f"/dev/sda1        100G   {pct}G    5G  {pct}% /data/logs"),
        ("err", f"{TODAY} 10:{(v % 50) + 10:02d}:58.330 ERROR  log write failed: No space left on device"),
        ("warn", f"{TODAY} 10:{(v % 50) + 10:02d}:58.410 WARN  log collector offset stale (paused {3 + v % 9}h)"),
        ("hl",  f"# 判读：磁盘 {pct}% + 采集停摆 -> 处置见 rb-{key}-disk-full；先清历史日志恢复采集"),
    ]


def _terminal_scene(i: int) -> dict:
    row = _svc_row(i)
    key, name = row[0], row[1]
    fm = _first_failmode(i)
    return dict(
        id=f"img-{key}-{fm}",
        title=f"{name} - kubectl logs / 现场命令输出",
        caption=f"{name} {fm_symptom_short(fm, name)} —— 与 rb-{key}-{fm} 对应",
        related=[f"rb-{key}-{fm}", f"svc-{key}", f"alert-{key}-lat", f"cmd-{key}"],
        lines=_terminal_lines(i),
    )


def _panel_scene(i: int) -> dict:
    row = _svc_row(i)
    key, name = row[0], row[1]
    code = row[7]
    cpu_thr = 70 + (i % 4) * 5
    mem_thr = 75 + (i % 4) * 5
    mem = 2 + (i % 4)
    lat_thr = 500 + (i % 5) * 100
    fm0 = _first_failmode(i)
    pod = f"{key}-7d9f8b6c4"
    return dict(
        id=f"img-{key}-panel",
        title=f"{name} - Grafana 资源水位面板",
        caption=f"{name} 资源水位告警 —— 与 alert-{key}-res / rb-{key}-{fm0} 对应",
        related=[f"alert-{key}-res", f"rb-{key}-{fm0}", f"svc-{key}", f"obs-{key}"],
        kind="panel",
        metrics=[
            [f"{key} QPS", f"{(2 + (i % 4)) * 800:,}", YELLOW, f"限流 {(2 + (i % 4)) * 1000:,} QPS"],
            [f"{key} P99", f"{lat_thr + 120}ms", RED, f"告警阈值 {lat_thr}ms"],
            [f"内存", f"{mem * 1024 - 87}MiB / {mem}Gi", RED, f"阈值 {mem_thr}%，已超"],
        ],
        table=[
            ["POD", "CPU", "MEMORY", "RESTARTS", "状态"],
            [f"{pod}-x2k9n", f"{cpu_thr + 9}%", f"{mem * 1024 - 87}MiB", "3", "内存贴顶"],
            [f"{pod}-m3p7q", f"{cpu_thr - 12}%", f"{mem * 1024 - 300}MiB", "0", "偏高"],
            [f"{pod}-k8w2r", f"{cpu_thr - 30}%", f"{mem * 512}MiB", "0", "正常"],
        ],
        annotation=(
            f"内存使用 {mem * 1024 - 87}MiB / {mem}Gi 已超 {mem_thr}% 阈值且持续上涨 -> 处置见 rb-{key}-{fm0}；"
            f"错误码 {code}-503-001 伴随出现；告警规则见 alert-{key}-res"),
    )


def extra_scenes() -> list[dict]:
    """60 个舰队服务 ×（终端 + 面板）= 120 张；与手写 20 张合计 140 张。

    必须覆盖全部 60 个服务：fleet_questions 的 multimodal 题按 i%6==0
    采样到第 55 号服务，只渲染前 42 个会让 3 道题的 image_ref 悬空
    （gen_eval 校验会 FAIL）。
    """
    scenes: list[dict] = []
    for i in range(60):
        scenes.append(_terminal_scene(i))
        scenes.append(_panel_scene(i))
    return scenes
