# 用半天到一天「熟悉自己的项目」——按面试官会问什么来读

> 前提：RAG 入库→检索你已经懂且实现过，所以**这块不安排时间**（只在被问时用 30 秒带过）。
> 目标不是"读完 3000 行代码"，而是三个可验收的产出：
> ① 白板画出一次请求的生命周期；② 3 分钟脱稿讲完；③ 20 个追问答得上来。

---

## 零、先定标准：什么叫"熟悉了"

不看代码的情况下，你能做到：

- 画出 10 个节点，并说出**哪两个是并行**、**哪三个是终局**、**缓存命中时从哪个节点直接结束**；
- 说出路由的 4 个类别和**判定优先级**（实体 > 寒暄；实时 + 知识 = hybrid）；
- 说出三层记忆分别是**什么载体**、Redis 挂了会发生什么（降级还是报错）；
- 说出 `exact → semantic → singleflight` 各自解决什么问题；
- 说出熔断和重试**谁在外层**、为什么；降级链有哪两个维度；
- 打开一条 trace，指出 TTFT 花在哪一段；打开大盘，说出顶行 ⓪ 为什么用 `chat_requests_total`。

**做不到就继续读；做到了就停，不要追求读完。**

---

## 一、半天版（4.5–5 小时，推荐先走这条）

按顺序做，每个 block 固定动作：**读指定行 → 跑一条命令 → 合上代码用自己的话复述**。
复述不出来就回到代码，不要跳到下一块。

### Block 0 · 先看运行中的东西（30 min）

不要从代码开始，从**一条真实请求**开始。

| 动作 | 命令 / 位置 |
|---|---|
| 发一条知识题，抓 SSE 帧 | `python -m scripts.selftest --only stream`（会打印每类帧） |
| 看一条完整 trace | `data/traces/2026-10-19.jsonl` 最后一行，`TurnTrace.stage` 的分段耗时 |
| 读事件协议的唯一出口 | `app/pipeline/orchestrator.py:88` `sse_frame()`；`app/api/chat.py:60-160` |

**必须能回答**
1. 一次请求会发哪几种帧？顺序是什么（meta 在哪、done 在哪）？
2. 为什么 `done` 之后前端不再消费，所以 degraded 必须发在 done 之前？

**答案**
1. 帧	谁发	关键字段
meta	cache_lookup / chitchat / singleflight	cache(hit/miss/bypass/singleflight)、route、kb_version、similarity、memory、trace_id
route	cache_lookup / chitchat（decision.as_event()）	intent、confidence、reason、signals、needs_rag/tool/write
docs	generate（orchestrator.py:1050）	sources、pages、scores、order_by、rank_scores、recall_sources、channels、fused
tool_call	_run_tools（:830）	name、args
tool_result	_run_tools（:837）	ok、source、attempts、status、simulated、degraded_from、breaker
confirm_required	_emit_pending（:702）	token、tool、args、risk、reason、expires_in、confirm_hint
notice	recall（:916）	stage=recall、code=rerank_disabled
token	generate / 缓存流 / chitchat	content（增量片段）
degraded	memory / cache / tools / recall / generate / pipeline	stage、reason、message
error	generate 失败 / 链路崩溃	message
done	四个终局节点
2. done是与客户端约定的结束标识，虽然不意味着服务端结束干活，当相当于影响 UI 的时间窗"的右边界，为了让降级可观测，所以必须在done之前发出

### Block 1 · 意图路由（40 min）

| 读什么 | 为什么读这里 |
|---|---|
| `app/pipeline/intent.py:225-300` `decide()` | 全部判定逻辑，60 行，**从上到下的优先级就是答案** |
| `app/pipeline/intent.py:46-133` 正则 | 实体（工单号/资产码/服务名/错误码）与实时信号长什么样 |
| `tests/test_intent.py` | 比 README 更快看懂「哪些边界被钉住了」 |

**必须能回答**
1. 「你好，INC-2026-0412 处理到哪了」为什么不是 chitchat？（实体优先于寒暄）
2. 什么情况下判 hybrid？为什么不直接让 tool 也带知识召回？ （1. 写操作 + 知识 2. 实时信号 + 知识）
3. 为什么规则路由能被评估，模型路由反而难？（纯函数、零依赖、可离线复现）

### Block 2 · LangGraph 编排（50 min，核心中的核心）

| 读什么 | 重点 |
|---|---|
| `orchestrator.py:1059-1086` | 图怎么连：**一条边看懂拓扑** |
| `node_route` 433 / `_after_route` 454 | 条件边：chitchat 直接终局 |
| `node_memory_load` 496 | 记忆为什么排在缓存前；失败为什么不抛 |
| `node_embed` 530 | 为什么 embedding 只算一次（缓存和检索共用） |
| `node_cache_lookup` 552 + `_after_cache_lookup` 631 | **命中就 END**；meta 为什么在这里发 |
| `node_singleflight` 635 + `_after_singleflight` 684 | 领跑者 / 跟随者的两条路 |
| `node_dispatch` 688 | 空节点，只为 fan-out：tools ∥ recall |
| `node_tools` 694 / `node_recall` 834 / `node_generate` 913 | 三段的输入输出契约 |
| `tests/test_orchestrator_graph.py` | 拓扑被测试钉死的部分 |

**必须能回答**
1. 哪两个节点并行？如果改成串行，耗时和失败面各有什么变化？ node_tools、node_recall并行
2. 缓存命中时，从哪个节点直接到 END？token 是谁推的？（`_stream_cached_frames` 回放，不是重新生成）
3. 为什么 `dispatch` 是空函数也要存在？（LangGraph 的 fan-out 需要一个汇合点）
4. `node_generate` 的 finally 里为什么要释放 singleflight 锁？

### Block 3 · 三层记忆（40 min）

| 读什么 | 重点 |
|---|---|
| `app/memory/__init__.py` | 文件头 20 行就是三层记忆的权威定义 |
| `session.py:115` `load` / `134` `save_turn` | Redis list 滑窗 + hash 事实；**异常吞掉返回错误串** |
| `session.py:191` `render_memory_context` | 渲染成"用户/助手"对，不是 JSON |
| `facts.py:27-69` | 规则抽事实：为什么不用模型抽（成本/确定性） |
| `tests/test_memory.py` + `selftest --only memory,memory_down` | 含 Redis 宕机场景 |

**必须能回答**
1. 三层分别是什么载体？为什么工作记忆不能放进程内？（横向扩展）
2. Redis 挂了，问答会失败吗？（不会：load 返回空、save 返回错误串 → `degraded{stage:memory}`）
3. 事实为什么要有 TTL 和条数上限？滑窗为什么不是日志？

### Block 4 · 两级缓存 + singleflight（40 min）

| 读什么 | 重点 |
|---|---|
| `app/cache/manager.py:493` `lookup` | exact → semantic 的顺序与短路 |
| `manager.py:206` `get_exact` / `381` `set_semantic` | key 里为什么带 `kb_version` |
| `manager.py:452` `acquire_singleflight` / `475` `wait_for_result` | 锁 + 等结果的具体形状 |
| `app/cache/guards.py` | 语义命中的护栏：防"看起来像其实不是" |
| `tests/test_cache_manager.py`、`test_cache_keys.py` | 键设计与并发行为 |

**必须能回答**
1. 语义缓存为什么会误命中？护栏怎么拦？
2. 并发 10 个相同问题，LLM 被调用几次？（1 次，其余 follower 回放）
3. 知识库更新后旧答案怎么失效？（`kb_version` 进 key，bump 即全副本失效）

### Block 5 · 可观测埋点（50 min）

| 读什么 | 重点 |
|---|---|
| `app/observability/metrics.py:66-190` | 18 个业务指标的名字与 label——**label 就是排障维度**（含安全沙箱的 `sandbox_decisions_total`） |
| `app/observability/tracing.py:327` `TurnTrace` / `371` `stage` / `409` `mark_ttft` / `422` `finish` | span 的边界 = 节点边界 |
| `app/api/admin.py:72` | 评估指标为什么要 POST 进 api 进程 |
| `monitoring/grafana/build_dashboard.py` | 面板是生成的，不是手写 JSON |
| Grafana 顶行 ⓪ | 近 5 分钟/1 小时请求数 |

**必须能回答**
1. TTFT 从什么时候开始算？为什么包含并发闸门排队？
2. 为什么评估指标不能用独立进程写？（prometheus_client 是进程内 registry，没人抓）
3. 为什么顶行用 `chat_requests_total` 而不是 `llm_requests_total`？
4. 指标名写错时 Grafana 会怎样？（不报错，画空线 —— 所以要守门测试）

### Block 6 · 高可用与工具接入（50 min）

| 读什么 | 重点 |
|---|---|
| `app/tools/resilience.py:134-300` `CircuitBreaker` | 三态、阈值、半开探测 |
| `resilience.py:315-380` `RetryPolicy` + `is_retryable` | **白名单**：只重试瞬态/超时/5xx |
| `resilience.py:424` `Hop` / `528` `summarize_hops` | 降级链怎么被记录下来 |
| `app/tools/base.py:119-180` `ToolRegistry.call` | 契约：**永不抛异常**，失败也返回 ToolResult |
| `app/tools/skills.yaml` | 声明式 skill：`when: previous_empty` 就是横向降级 |
| `app/tools/mcp_client.py` | JSON-RPC over HTTP，和 Java 侧对应 |
| `java-mcp/.../McpController.java` + `ChaosFilter.java` | 5 个工具 + `X-Chaos` 注入 |

**必须能回答**
1. 熔断和重试谁在外层？为什么？（重试会放大流量，熔断先挡住）
2. 「纵向降级」和「横向降级」各是什么？各举一个本项目的例子。
3. 为什么 `ToolRegistry.call` 不允许抛异常？（上层是 SSE 流，抛了就断流）
4. 停掉 java-mcp 后，用户会看到什么？（答案仍给出，但 `source=mock` + degraded 帧，且不伪装成实时数据）

### Block 7 · 收口（30 min）

- 合上所有文档，白板画图（10 节点 + 两条降级链）；
- 对着手机录音讲 3 分钟，回放听哪里卡；
- 打开 `docs/DELIVERY_D2.md` 的「高频追问速答」，遮住右列自答。

**半天版时间账**：30+40+50+40+40+50+50+30 = **5 小时 20 分**。
时间只有 4 小时就砍 Block 4 的内部实现（只记 `exact→semantic→singleflight` 三层结论）和 Block 6 的 Java 细节。

---

## 二、一天版（8 小时，加三个动手实验）

上午走完半天版的 Block 0–3（约 3 小时），下午做实验 + 补齐 Block 4–6。

### 实验 1 · 改语义阈值，看命中率怎么变（40 min）

- 位置：`.env` 的 `CACHE_SEMANTIC_THRESHOLD`（当前 0.96，读取处见 `app/core/config.py:102` 与 `app/cache/guards.py`）；
- 做法：把阈值调高 0.03（→0.99），重建 api，问 3 个**改写过的**问题（同义不同词）；
- 记录：命中/未命中、`similarity` 值、护栏拒绝数（`cache_guard_rejected_total`）；
- 结论要能说：**阈值是 precision/recall 的权衡，不是越高越好**。

### 实验 2 · 加一个工具或一个 skill step（60 min）

- 最省事：在 `app/tools/providers.py` 的 mock 里加一个工具 + 在 `app/tools/skills.yaml` 加一条声明；
- 必须同步改 `ToolSpec`（签名进 prompt）和至少一条测试；
- 跑 `mcp_probe` 不需要动 Java，但你要能说清「mock 和 MCP 同签名」是结构保证的。

### 实验 3 · 跑一轮故障演练并读报告（60 min）

```powershell
py -3.12 -m scripts.fault_drill --faults stop-qdrant,stop-redis --rounds 1 --run
```

> 不加 `--run` 是 dry-run（只打印计划）；默认 faults 里还有 stop-mysql，本项目没起 MySQL，务必显式指定。
> 演练会真的 `docker stop` 依赖，别在演示前跑。

- 读 `eval_output/fault_drill_*.json` 的 `definition` 字段（口径全在那）；
- 对照 Grafana：`降级应答`、`熔断状态`、`工具来源` 三个面板；
- 重点理解：**为什么 stop-qdrant 的可用性是 100%，而 stop-redis 恢复要 162 秒**。

### 下午剩余时间

- Block 4（缓存内部）40 min、Block 5（可观测）50 min、Block 6（韧性/MCP）50 min；
- 最后 30 min：录音复述 + 20 题自测。

**一天版时间账**：上午 3h + 实验 2h40 + 三个 block 2h20 ≈ **8 小时**。

---

## 三、20 道自测题（先自答，再对照要点）

| # | 问题 | 要点 |
|---|---|---|
| 1 | 10 个节点顺序？ | route → (chitchat) / memory_load → embed → cache_lookup → singleflight → dispatch → tools∥recall → generate |
| 2 | 哪两个节点并行？为什么能并行？ | tools 与 recall 无数据依赖；hybrid 才两路都跑 |
| 3 | 缓存命中走哪条边？ | cache_lookup → END，token 由 `_stream_cached_frames` 回放 |
| 4 | meta 帧为什么在 cache_lookup 发？ | 命中与否决定 meta 的 `cache` 字段；它必须早于 route/docs |
| 5 | 路由四类？ | rag / tool / hybrid / chitchat；实体优先，实时+知识=hybrid |
| 6 | 为什么规则路由可评估？ | 纯函数、无 IO、可离线复现，同一份 `decide()` 服务线上与评估 |
| 7 | 三层记忆载体？ | LangGraph state / Redis list 滑窗 / Redis hash 事实 |
| 8 | Redis 挂了会怎样？ | 记忆降级但问答继续；`degraded{stage:memory}` 可见 |
| 9 | 事实为什么规则抽取？ | 确定、零成本、可测；召回错误代价低（只是背景提示） |
| 10 | 三级缓存防线？ | exact → semantic(KNN+护栏) → singleflight |
| 11 | singleflight 锁何时释放？ | `node_generate` 的 finally，成败都释放 |
| 12 | kb_version 作用？ | 进缓存 key；bump 后旧答案自然不命中，全副本一致 |
| 13 | 熔断三态？ | closed / open / half_open；半开只放一个探测 |
| 14 | 重试白名单？ | 瞬态、超时、连接、5xx；4xx 与业务错误不重试 |
| 15 | 两种降级维度？ | 纵向：同工具多数据源（mcp→cache→mock）；横向：同意图多路径（skill fallback） |
| 16 | `ToolRegistry.call` 契约？ | 永不抛，失败返回 ToolResult(error)；保证 SSE 不断流 |
| 17 | 降级为什么必须可见？ | 否则 mock 数据会被当成实时数据，答案"看起来正常"最危险 |
| 18 | 请求数用哪个指标？ | `chat_requests_total`（HTTP 层）；`llm_requests_total` 只是 LLM 调用数 |
| 19 | 评估指标为什么 POST 进 api？ | 独立进程的 registry 不会被 Prometheus 抓取 |
| 20 | 项目最大短板？ | embed 在缓存前（hot≈cold）；慢故障无告警；未接 RAGAS |

---

## 四、读代码的方法（省一半时间）

1. **先读测试，再读实现**：`tests/test_intent.py`、`test_orchestrator_graph.py`、`test_memory.py`、`test_resilience.py` 把这几个模块的契约写死了，比正文短得多。
2. **只读函数签名和 docstring**：本项目 docstring 写的是「为什么」，正文是「怎么做」；第一遍只要「为什么」。
3. **遇到长函数，只看 `return` 和 `raise`**：`node_recall`（80 行）实际只有 3 条出口。
4. **不要线性读 orchestrator.py 1098 行**：按 Block 2 的表格跳读，节点之间互相独立。
5. **每读完一块，立刻用一条命令验证**：能跑出来的理解才算数（selftest 的 `--only` 就是为此准备的）。
6. **把踩过的坑当记忆锚点**：`rerank:disabled` 假降级、`llm_requests_total` 误当请求数、nginx 只解析一次、uid 不匹配 —— 这四个故事比任何概念都好记，而且面试官爱听。

---

## 五、时间不合理的判断

- **半天足够**「能讲、能答常规追问」，但不足以覆盖缓存内部实现和 Java MCP 细节；
- **一天是舒适区**：能加三个实验，面试时可以说「我改过阈值/加过工具/跑过演练」，可信度完全不同；
- **不建议超过一天**：项目细节的边际收益很快递减，剩下的时间应该拿去练讲解（录音 3 遍比多读 300 行有用）。

**最低配版本（只有 3 小时）**：Block 0 + Block 2 + Block 3 + 背 20 题。
这三块覆盖了面试 80% 的追问（编排、记忆、请求生命周期）。
