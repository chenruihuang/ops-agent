"""Locust 压测脚本 —— TTFT / 并发 / 缓存命中率三个指标都由它产出。

为什么不用普通 HTTP 压测工具
----------------------------
TTFT 的定义是"从发出请求到收到**第一个 token**"，不是首字节。
普通压测工具只能测到响应体第一个字节，而 SSE 的第一个字节往往是
`meta` 或 `docs` 事件（检索结果），不是 token。必须解析事件流才测得准。

跑法
----
    # Web UI 交互式
    docker compose --profile loadtest up -d locust
    # 打开 http://localhost:8089

    # 无头 + 阶梯加压（推荐，用于产出可复现的数字）
    locust -f loadtest/locustfile.py --host http://nginx --headless \
           --users 120 --spawn-rate 5 --run-time 6m --html report.html

    # 走阶梯形状（脚本里定义好的 10→30→50→80→120）
    locust -f loadtest/locustfile.py --host http://nginx --headless \
           --run-time 6m --html report.html

读结果的方法
------------
Locust 报告里会出现两类行：
    chat/stream   —— 端到端总耗时（P95 用于"端到端延迟"）
    TTFT          —— 首 token 延迟，**并发数取这一行的 P95 < 3000ms 的最大档**

"最大并发数"的判定口径写死为：
    错误率 < 1% 且 TTFT P95 < 3000ms 时，能撑住的最大用户数。
这个口径必须写进简历/README，否则数字没有意义。
"""

from __future__ import annotations

import json
import os
import random
import time

import requests
from locust import HttpUser, LoadTestShape, between, events, task

API_KEY = os.getenv("API_KEY", "demo-key-change-me")

# 高频问题：反复问，用来把缓存命中率打上去（企业场景里这类问题占比本来就在 30%+）。
#
# ⚠️ 这里踩过一个坑，值得写下来：初版这 5 条是**法律问题**（"诉讼时效是几年"、
# "租房超过六个月没签书面合同会怎样"）—— 从上一个项目 cs-agent 抄过来忘了改。
# 后果比"看着别扭"严重得多：本项目的语料是运维手册，法律问题 100% 查不到，
# 于是每次请求都要走完整链路、都要调 LLM、都要写缓存，答案固定是"资料未涉及"。
# 压出来的 TTFT 与缓存命中率**全都是失真的**，而这个错误不会报任何警。
# 所以压测问题必须来自被测语料的真实领域。
HOT_QUESTIONS = [
    "ORD-429-001 是什么含义",
    "订单服务的单用户下单限流是多少",
    "连接池耗尽该怎么排查",
    "P1 告警的响应时限是多久",
    "缓存穿透和缓存雪崩怎么区分",
]

# 冷问题：每次略有不同，制造缓存未命中，用来观察真实链路延迟
COLD_TEMPLATES = [
    "{}的处置步骤是什么",
    "排查{}应该先看什么",
    "{}的常见根因有哪些",
    "{}的阈值是怎么定的",
]

COLD_TOPICS = [
    "连接池耗尽", "慢查询", "TLS 证书过期", "磁盘 inode 耗尽",
    "线程池打满", "缓存雪崩", "MQ 消息积压", "ES 分片未分配",
]


def _one_event(raw: str) -> dict | None:
    if not raw or not raw.startswith("data: "):
        return None
    body = raw[6:].strip()
    if body == "[DONE]":
        return {"type": "done"}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def stream_chat(question: str, host: str, timeout: float = 120.0):
    """返回 (ttft_seconds, total_seconds, cache_source, token_count, route, degraded)。

    ttft_seconds 为 None 表示整个流里一个 token 都没收到（应当视为失败）。
    route / degraded 一起返回，是因为压测要回答的不只是"快不快"，
    还有"分流对不对"（路由分布）和"降级了多少次"（可用性）。
    """
    t0 = time.perf_counter()
    ttft = None
    cache_source = "unknown"
    route = "unknown"
    degraded = 0
    tokens = 0

    with requests.post(
        f"{host}/chat/stream",
        json={"message": question},
        headers={"X-API-Key": API_KEY, "Accept": "text/event-stream"},
        stream=True,
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            event = _one_event(raw or "")
            if not event:
                continue
            kind = event.get("type")
            if kind == "meta":
                cache_source = event.get("cache", "unknown")
                route = event.get("route", "unknown")
            elif kind == "route":
                route = event.get("intent", route)
            elif kind == "degraded":
                degraded += 1
            elif kind == "token":
                if ttft is None:
                    ttft = time.perf_counter() - t0
                tokens += 1
            elif kind == "done":
                break

    return ttft, time.perf_counter() - t0, cache_source, tokens, route, degraded


class OpsChatUser(HttpUser):
    wait_time = between(0.2, 1.0)

    @task(6)
    def ask_hot(self):
        self._ask(random.choice(HOT_QUESTIONS), label="hot")

    @task(4)
    def ask_cold(self):
        template = random.choice(COLD_TEMPLATES)
        topic = random.choice(COLD_TOPICS)
        question = template.format(topic) if "{}" in template else template
        if random.random() < 0.3:
            question = f"{question}（第{random.randint(1, 5)}次问）"
        self._ask(question, label="cold")

    def _ask(self, question: str, label: str) -> None:
        start = time.perf_counter()
        try:
            ttft, total, cache_source, tokens, route, degraded = stream_chat(question, self.host)
            if ttft is None:
                raise RuntimeError("未收到任何 token")

            # 端到端总耗时 —— 计入 Locust 主统计
            self.environment.events.request.fire(
                request_type="SSE",
                name=f"chat/stream[{label}]",
                response_time=total * 1000,
                response_length=tokens,
                exception=None,
                context={},
            )
            # 首 token 延迟 —— 单独一行，这样 P95 的口径不会被总耗时掩盖
            self.environment.events.request.fire(
                request_type="TTFT",
                name=f"first_token[{label}]",
                response_time=ttft * 1000,
                response_length=0,
                exception=None,
                context={},
            )
            STATS["cache"][cache_source] = STATS["cache"].get(cache_source, 0) + 1
            STATS["route"][route] = STATS["route"].get(route, 0) + 1
            STATS["degraded"] += degraded
            STATS["ttft"].append(ttft * 1000)
        except Exception as exc:  # noqa: BLE001
            self.environment.events.request.fire(
                request_type="SSE",
                name=f"chat/stream[{label}]",
                response_time=(time.perf_counter() - start) * 1000,
                response_length=0,
                exception=exc,
                context={},
            )


STATS: dict = {"cache": {}, "route": {}, "ttft": [], "degraded": 0}


@events.test_stop.add_listener
def _report(environment, **_kwargs):
    """压测结束打印缓存命中率与路由分布 —— 这些数要直接抄进 README。

    为什么要一起报路由分布：命中率与路由是耦合的。
    如果 90% 的问题都走了 chitchat（不查缓存、不检索），
    那"命中率很低"这个结论就是假的 —— 分母里混进了根本不参与缓存的请求。
    """
    total = sum(STATS["cache"].values())
    if not total:
        return
    print("\n" + "=" * 56)
    print("缓存命中分布（本次压测）")
    for source, count in sorted(STATS["cache"].items(), key=lambda x: -x[1]):
        print(f"  {source:<12} {count:>6}  {count / total:.1%}")
    hit = (
        STATS["cache"].get("exact", 0)
        + STATS["cache"].get("semantic", 0)
        + STATS["cache"].get("singleflight", 0)
    )
    print(f"  综合命中率        {hit / total:.1%}")

    print("-" * 56)
    print("路由分布")
    for route, count in sorted(STATS["route"].items(), key=lambda x: -x[1]):
        print(f"  {route:<12} {count:>6}  {count / total:.1%}")
    if STATS["degraded"]:
        print(f"  降级事件          {STATS['degraded']} 次"
              f"（依赖异常时发生；应为 0 或极少，否则说明有链条不稳定）")

    if STATS["ttft"]:
        ordered = sorted(STATS["ttft"])
        print("-" * 56)
        print(f"  TTFT P50          {ordered[len(ordered) // 2]:.0f} ms")
        print(f"  TTFT P95          {ordered[int(len(ordered) * 0.95)]:.0f} ms")
    print("=" * 56)


class StagedShape(LoadTestShape):
    """阶梯加压：取"错误率<1% 且 TTFT P95<3s"能撑住的最大档位作为并发数上限。"""

    stages = [
        {"at": 60, "users": 10, "spawn_rate": 5},
        {"at": 120, "users": 30, "spawn_rate": 5},
        {"at": 180, "users": 50, "spawn_rate": 5},
        {"at": 240, "users": 80, "spawn_rate": 5},
        {"at": 300, "users": 120, "spawn_rate": 10},
        {"at": 360, "users": 0, "spawn_rate": 10},
    ]

    def tick(self):
        elapsed = self.get_run_time()
        for stage in self.stages:
            if elapsed < stage["at"]:
                return stage["users"], stage["spawn_rate"]
        return None
