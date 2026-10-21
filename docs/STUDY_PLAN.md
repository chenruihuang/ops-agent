# 用半天到一天「熟悉自己的项目」——按面试官会问什么来读

> 前提：RAG 入库→检索你已经懂且实现过，所以**这块不安排时间**（只在被问时用 30 秒带过）。
> 目标不是"读完 3800 行代码"，而是三个可验收的产出：
> ① 白板画出一次请求的生命周期；② 3 分钟脱稿讲完；③ 20 个追问答得上来。
>
> **行号说明**：文中所有 `file:line` 是 2026-10-20 的快照，代码一动就会漂。
> 复述卡住时**先按函数名搜**（`grep -n "def 函数名" app/...`），不要信行号。

---

## 零、先定标准：什么叫"熟悉了"

不看代码的情况下，你能做到：

- 画出 13 个节点，并说出**哪两个是并行**、**哪四个是终局**、**缓存命中时从哪个节点直接结束**、为什么缓存要拆 exact/semantic 两段；
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
每道题先遮住「答」自测，答不上再展开看。

### Block 0 · 先看运行中的东西（30 min）

不要从代码开始，从**一条真实请求**开始。

| 动作 | 命令 / 位置 |
|---|---|
| 发一条知识题，抓 SSE 帧 | `python -m scripts.selftest --only stream`（会打印每类帧） |
| 看一条完整 trace | `data/traces/<今天日期>.jsonl` 最后一行，`TurnTrace.stage` 的分段耗时（注意：该目录有已知的**多副本并发写损坏**问题，个别行解析不了属正常） |
| 读事件协议的唯一出口 | `app/pipeline/orchestrator.py:95` `sse_frame()`；端点只透传 `app/api/chat.py:99-186` |

**必须能回答**

1. **一次请求会发哪几种帧？顺序是什么（meta 在哪、done 在哪）？**
   - **答**：10 种 data 帧：`meta`（第一帧，cache/route/kb_version/trace_id）→ `route`（intent/confidence/needs_write）→ 之后按路径分支：`tool_call`/`tool_result`（工具路）、`confirm_required`（挂起路）、`notice`/`degraded`（降级路）、`docs`（生成前）、`token`（逐片）、`error`（失败收尾）、`done`（最后一个 data 帧，cache/route/stage_ms/total_ms，挂起时带 status+pending）。另有 `: keepalive` 注释帧（15s 一个，onmessage 收不到）和 `data: [DONE]` 终止符。五条路径：闲聊 = meta→route→token→done；缓存命中 = meta(hit)→route→token→done；RAG = meta→route→docs→token→done；工具 = meta→route→tool_call×N→tool_result×N→token→done；挂起 = meta→route→tool_call→tool_result(pending)→confirm_required→done(awaiting_confirmation)，**没有 token 帧**。`meta` 永远第一（route 节点不发帧；D4 后 meta 由缓存节点发——exact 命中在 cache_exact、miss 路径在 cache_semantic，因为 cache 结论只有查完缓存才知道）；`done` 永远最后、后面只跟 `[DONE]`；hybrid 的 tools∥recall 帧会交错，只能断言相对顺序。
2. **为什么 `done` 之后前端不再消费，所以 degraded 必须发在 done 之前？**
   - **答**：done 是与客户端约定的结束标识——服务端 done 后**还会干活**（generate 的 finally 里释放单飞锁），但不再发帧。三层原因：① 客户端真读不到：`stream_chat.py` 收到 `[DONE]` 直接 break，EventSource close 后不再派发，OpenAI 兼容 SDK 把 `[DONE]` 当迭代结束——done 后的帧不是"被忽略"是"根本没被读"；② done 是结算点，前端收到即做终态渲染（关 loading、按 status 弹确认框），degraded 要求**修改正在展示的内容**，晚到就只能丢；③ 这条约束反向决定了代码结构：`_save_turn` 必须 await 在 done 之前（哪怕慢 20ms），否则"记忆写失败"永远发不出去，用户以为这轮被记住了。本质：**done 是"还能影响 UI 的时间窗"的右边界，降级必须可观测，所以必须落在窗内**。

### Block 1 · 意图路由（40 min）

| 读什么 | 为什么读这里 |
|---|---|
| `app/pipeline/intent.py:294-391` `decide()` | 全部判定逻辑，**从上到下的优先级就是答案** |
| `app/pipeline/intent.py:46-158` 正则 | 实体（工单号/资产码/服务名/错误码）、实时信号、`WRITE_ACTION:119`、`KNOWLEDGE:154` |
| `app/pipeline/intent.py:186` `_write_clause` | 分句否决：知识词只否决**写动词所在分句** |
| `tests/test_intent.py` | 比 README 更快看懂「哪些边界被钉住了」 |

**必须能回答**

1. **「你好，INC-2026-0412 处理到哪了」为什么不是 chitchat？**
   - **答**：闲聊判定是"整句由问候/身份短语组成**且无业务实体**"（`intent.py:304`）。实体优先于寒暄：工单号 + 进度词（"处理到哪"）是强实时信号。设计不对称：闲聊题误判成 rag，用户拿到资料重问一遍就好；业务题误判成闲聊，用户拿到"你好"完全不知所措。
2. **什么情况下判 hybrid？为什么不直接让 tool 也带知识召回？**
   - **答**：hybrid 两个入口。① 写操作 + 知识（`intent.py:364-376`，conf 0.86）：写动词（重启/扩容/副本数…）+ 服务名 + **写动词分句内无知识词**（`_write_clause` 分句否决，"重启 order-service 的步骤是什么"否决写、落回 rag）+ 整句有知识词；② 实时 + 知识（`:383-387`，conf 0.85）：live 信号（工单号/资产码/查询动词/时态+对象/进度词任一）+ `KNOWLEDGE` 命中（为什么/怎么/阈值/多少…，「哪些」「是什么」刻意排除——否则 tool 题大量误判）。不让 tool 一律带召回的四条理由：**事实混写**（手册一般性说法与实时数据同语气，错的像对的）；**指标污染**（没被用的片段全进 precision 分母，检索退化无法归因）；**降级淹没**（recall 挂了对 tool 答案毫无影响，报 degraded 是噪音——与 rerank:disabled 假降级同类）；**纯写挂起白跑**（await_confirm 不生成答案，召回纯浪费）。注意编码成本不是理由——D4 之前 `node_embed` 对所有非闲聊都跑本来就在付；D4 拆段后编码只由**未命中**请求支付（exact 命中零编码），这条理由进一步弱化。代价：KNOWLEDGE 漏判时（"该不该"这类词）hybrid 判成 tool，知识一半静默消失，这是唯一"静默丢答案"的路径，靠 52 条标注校准。
3. **为什么规则路由能被评估，模型路由反而难？**
   - **答**：`decide()` 是纯函数、零 IO、可离线复现——同一份函数既服务线上又服务评估（`evaluate_routing`，`intent.py:406`），52 条标注可反复跑出准确率与混淆矩阵。模型路由的准确率随模型版本漂移，评估一次花一次钱，且给不出"命中了哪个信号"这种可解释理由。LLM 兜底只在低置信度时开（默认关）——先有规则基线才知道兜底值不值那 500ms。

### Block 2 · LangGraph 编排（50 min，核心中的核心）

| 读什么 | 重点 |
|---|---|
| `orchestrator.py` 末尾 `build_graph` | 图怎么连：**30 行看懂全部拓扑** |
| `node_route` / `_after_route` | 条件边：chitchat 直接终局 |
| `node_memory_load` | 记忆为什么排在缓存前；失败为什么不抛 |
| `node_embed` 544 | 为什么 embedding 只算一次（语义缓存与检索共用）；**D4 后只有未命中请求才付编码费** |
| `node_cache_exact` 602 + `_after_cache_exact` 657 | 缓存第 1 段：只查 exact，**命中就 END 且零编码**；miss 静默（不发帧） |
| `node_cache_semantic` 661 + `_after_cache_semantic` 716 | 缓存第 2 段：语义查询 + **meta/route 在这里统一发**（miss 路径） |
| `node_singleflight` 720 + `_after_singleflight` | 领跑者 / 跟随者的两条路 |
| `node_dispatch` | 空节点，只为分叉：tools ∥ recall |
| `node_tools_done` + `_after_tools_done` | 汇合点：挂起要在**两路都完成后**才改道 |
| `node_tools` / `node_recall` / `node_generate` | 三段的输入输出契约 |
| `tests/test_orchestrator_graph.py` | 拓扑被测试钉死的部分（13 节点） |

**必须能回答**

1. **哪两个节点并行？如果改成串行，耗时和失败面各有什么变化？**
   - **答**：`dispatch` 之后分出的 `tools` 与 `recall`（`:1297-1298`）。耗时：并行 = max(两段)，串行 = 两段相加，收益 = min/max——两段量级相近时才明显。失败面三个变化：① **总超时预算被压缩**，原来刚好在预算内的 hybrid 请求从"慢但成功"变"失败"；② **单飞退化**：leader 持锁时间从 max 变 sum，而跟随者只等 6s（`cache_singleflight_wait`），超时后全部自己算，并发放大；③ **失败局部性丢失**：现在一路坏只丢半份答案，串行后先跑的那段占满预算，后段更容易超时——"半份"退化成"零"。串行唯一收益：帧顺序从"交错不可断言"变"确定可断言"。
2. **缓存命中时，从哪个节点直接到 END？token 是谁推的？**
   - **答**：缓存命中走两段：`cache_exact` 命中 → END（**零编码**，D4 后 exact 命中只做一个 Redis GET）；exact 未命中 → `embed` → `cache_semantic` 语义命中 → END。token 由 `_stream_cached_frames` 推：`split_text` 把 Redis 里存的完整答案按 24 字符切片逐片 yield，最后补 done + `[DONE]`——这是**回放不是重新生成**，省掉的是整个生成段，讲的时候要说清是"假流式"。TTFT 在第一片处测，因为缓存命中的全部卖点就是首字快。singleflight 跟随者走同一个生成器，两条路径的 done 字段不会漂移。真机口径：exact 命中 TTFT 21–26ms（修复前 hot P50 6668ms ≈ cold，命中优势被 6.33s 编码吃干）。
3. **为什么 `dispatch` 是空函数也要存在？**
   - **答**：因为上游是**条件边**（`_after_cache_exact`/`_after_cache_semantic`/`_after_singleflight`），条件边返回值只能指定**一个**目标；要从一个条件出口变成两条并行分支，必须经过一个普通节点——普通节点挂多条出边就是扇出。所以 dispatch 是**分叉点**，`return {}` 就是它的全部工作。注意与汇合点是镜像的一对：`tools_done` 把两路合回一条**并在合流后做条件判定**——条件边若直接挂 tools 上，recall→generate 那条边仍会触发，"边等确认边生成已为你重启"（`node_tools_done` 注释记的实测事故）。附带好处：trace 里 tools/recall 的 span 都以 dispatch 为父，并行耗时才有归处；以后加第三分支只加一条边。
4. **`node_generate` 的 finally 里为什么要释放 singleflight 锁？**
   - **答**：锁在 `node_singleflight:660` 获取（TTL 30s），释放点必须在**结果写入缓存之后**（`cache.store` 在 `:1118`）——早一步，跟随者醒来仍是 miss，等 6s 后自己也跑，单飞失效。放 finally 是因为 generate 有三条离开路径：正常完成、LLM 异常（`:1096` 被吞）、客户端断开（CancelledError）——漏一条，锁就活满 30s，期间同问题请求全成跟随者白等。**已知缺口**：纯写挂起路径（needs_rag=False → await_confirm → END）不经过 generate，`release_singleflight` 全文只有 `:1170` 一处，所以那条路锁靠 30s TTL 兜底，期间同问题请求每人白等 6s（挂起路径永不写缓存）。根因：LangGraph 节点间没有共享的异步上下文，`async with` 跨不了节点，只能靠"每个终局自己记得"——靠约定不靠结构，迟早漏。

### Block 3 · 三层记忆（40 min）

| 读什么 | 重点 |
|---|---|
| `app/memory/__init__.py` | 文件头 20 行就是三层记忆的权威定义 |
| `session.py:67` `load` / `78` `save_turn` | Redis list 滑窗 + hash 事实；**异常吞掉返回错误串** |
| `session.py:191` `render_memory_context` | 渲染成"用户/助手"对，不是 JSON |
| `app/memory/facts.py`（全文 69 行） | 规则抽事实：为什么不用模型抽（成本/确定性） |
| `tests/test_memory.py` + `selftest --only memory,memory_down` | 含 Redis 宕机场景 |

**必须能回答**

1. **三层分别是什么载体？为什么工作记忆不能放进程内？**
   - **答**：本轮对话内的 state（LangGraph AgentState）→ 会话历史（Redis list 滑窗）→ 长期事实（Redis hash，带 TTL）。工作记忆放进程内，nginx 一轮询下一个请求就打到另一副本，"刚才说到哪了"直接丢失——多副本下凡是"跨请求的状态"都必须进共享存储。
2. **Redis 挂了，问答会失败吗？**
   - **答**：不会。`load` 异常被兜住返回空上下文、`save_turn` 返回错误串，由携带 meta 的节点补发 `degraded{stage:memory}`（meta 必须仍是第一帧，所以由它补发而不是 load 时发）。用户本轮照常得到答案，只是不带历史——锦上添花的依赖不能拖垮主链路。
3. **事实为什么要有 TTL 和条数上限？滑窗为什么不是日志？**
   - **答**：事实是给 LLM 看的背景提示，不是审计记录——过期的事实（"用户在用 v1 版本"）比没有事实更危险，因为模型会当真；上限防止上下文预算被老事实挤占。滑窗保留最近 N 轮对话而不是全量日志，同理：预算有限，"三个月前聊过什么"对当前回答是噪音。

### Block 4 · 两级缓存 + singleflight（40 min）

| 读什么 | 重点 |
|---|---|
| `app/cache/manager.py:502` `lookup` | exact → semantic 的顺序与短路 |
| `manager.py:215` `get_exact` / `390` `set_semantic` | key 里为什么带 `kb_version` |
| `manager.py:461` `acquire_singleflight` / `484` `wait_for_result` | 锁（SET NX PX 30s）+ 轮询等结果（150ms 一次，最多 6s） |
| `app/cache/guards.py` | 语义命中的护栏：防"看起来像其实不是" |
| `tests/test_cache_manager.py`、`test_cache_keys.py` | 键设计与并发行为 |

**必须能回答**

1. **语义缓存为什么会误命中？护栏怎么拦？**
   - **答**：KNN 按 embedding 距离找"最像的"，但"两个工单分别处理到哪了"这类问法向量上几乎一样、答案却完全不同。护栏（guards.py）在命中后做二次校验：实体（工单号/资产码）不一致直接拒、相似度低于阈值拒、拒了记 `cache_guard_rejected_total`。所以语义阈值 0.96 这么高——它是 precision 取向的。
2. **并发 10 个相同问题，LLM 被调用几次？**
   - **答**：1 次。第一个拿到 SET NX 锁的是 leader，跑完整链路并写缓存；其余 9 个是 follower，在 `wait_for_result` 里每 150ms 查一次缓存，命中后走 `_stream_cached_frames` 回放。follower 最多等 6s，超时自己算（兜底：leader 挂了不能拖死所有人）。注意锁的 key 是**归一化后的问题**，刻意不含 kb_version——锁是秒级的，跨版本复用不影响正确性。
3. **知识库更新后旧答案怎么失效？**
   - **答**：`kb_version` 进缓存 key（`core/config.py:116`）。bump 之后旧 key 天然查不到，不用逐条清理，且对多副本天然一致（大家读同一个版本号）。运行时版本从 Redis 读而不是 settings——settings 是启动快照，bump 一次事件流里的版本号就对了。

### Block 5 · 可观测埋点（50 min）

| 读什么 | 重点 |
|---|---|
| `app/observability/metrics.py:68-196` | 20+ 个业务指标的名字与 label——**label 就是排障维度**（含沙箱的 `sandbox_decisions_total:166`、`chat_requests_total:117`、`ttft_seconds:97`） |
| `app/observability/tracing.py:327` `TurnTrace` / `371` `stage` / `409` `mark_ttft` / `422` `finish` | span 的边界 = 节点边界 |
| `app/api/admin.py:72` | 评估指标为什么要 POST 进 api 进程 |
| `monitoring/grafana/build_dashboard.py` | 面板是生成的，不是手写 JSON；守门测试 11 条 |
| Grafana 顶行 ⓪ | 近 5 分钟/1 小时请求数 |

**必须能回答**

1. **TTFT 从什么时候开始算？为什么包含并发闸门排队？**
   - **答**：从收到 HTTP 请求算起（`t0` 在端点入口打点），含 `_semaphore` 排队时间。trace 句柄也在端点创建——否则"P95 慢"里那段排队会被算丢，看起来像后端没问题。测量点只有一处：第一个 token 片到达时（缓存路在第一片回放处，生成路在首个 delta 处）。
2. **为什么评估指标不能用独立进程写？**
   - **答**：prometheus_client 的 registry 是进程内的。评估脚本在另一个进程 inc 计数器，api 进程的 /metrics 根本看不到——"有数但数错了"的典型。所以评估指标必须 POST `/admin/eval/metrics` 写进 api 进程；gauge 重启清零，发布时落盘、启动时回填。
3. **为什么顶行用 `chat_requests_total` 而不是 `llm_requests_total`？**
   - **答**：前者是 HTTP 层（含缓存短路、断连），后者只是 LLM 调用数。缓存命中率 75% 时后者只有前者的 1/4——用它做流量自检，空闲时段会误报"无流量"。
4. **指标名写错时 Grafana 会怎样？**
   - **答**：不报错，面板画成"无数据"，且 noValue 会把"查询失败"渲染成"无流量"——和真没流量长得一样。所以面板是脚本生成的 + 守门测试：指标名存在、有写入点（防死面板）、有 noValue 处理。

### Block 6 · 高可用与工具接入（50 min）

| 读什么 | 重点 |
|---|---|
| `app/tools/resilience.py:134-310` `CircuitBreaker` | 三态、阈值、半开探测 |
| `resilience.py:315-360` `RetryPolicy` + `is_retryable:328` | **白名单**：只重试瞬态/超时/5xx |
| `resilience.py:424` `Hop` / `528` `summarize_hops` | 降级链怎么被记录下来 |
| `app/tools/base.py:181` `ToolRegistry.call` | 契约：**永不抛异常**，失败也返回 ToolResult |
| `app/tools/skills.yaml` | 声明式 skill：`when: previous_empty` 就是横向降级 |
| `app/tools/mcp_client.py` | JSON-RPC over HTTP，和 Java 侧对应 |
| `java-mcp/.../McpController.java` + `ChaosFilter.java` | 5 个工具 + `X-Chaos` 注入 |

**必须能回答**

1. **熔断和重试谁在外层？为什么？**
   - **答**：熔断在外、重试在内。重试天然放大流量（1 次调用变 N 次），如果重试在外，下游已经过载时重试会把放大后的流量全打过去；熔断在外则先挡住——打开状态直接快速失败，连重试的机会都不给，下游才有机会恢复。
2. **「纵向降级」和「横向降级」各是什么？各举一个本项目的例子。**
   - **答**：纵向 = 同一工具多个数据源：MCP 挂了 → 缓存（只读 30s 内）→ mock，Hop 记录每一跳，`degraded_from` 进事件流。横向 = 同一意图多条路径：skill 某步 `previous_empty` 时按声明切到下一条路径。前者解决"这个数据从哪来"，后者解决"这个意图还能怎么达成"。
3. **为什么 `ToolRegistry.call` 不允许抛异常？**
   - **答**：上层是 SSE 流。抛异常会打断流式响应，用户看到连接中断；返回 `ToolResult(ok=False)` 则降级逻辑照常走、degraded 帧照常发。契约写在签名上（`-> ToolResult`，无 raises），测试钉死。
4. **停掉 java-mcp 后，用户会看到什么？**
   - **答**：答案仍给出（mock 兜底），但 `tool_result.source=mock` + `degraded` 帧 + 熔断器 open。关键是**不伪装**：用户明确知道这条数据不是实时的。演练实测 stop-qdrant 可用性 100%（BM25 单路仍在），stop-redis 恢复要 30s+（语义缓存与锁全挂）。

> **写工具（安全沙箱）单独走了 `docs/STUDY_SANDBOX.md`**：它给图加了 `tools_done` +
> `await_confirm` 两个节点，又在 `_make_handler` 前面插了一道闸门，串在 Block 2 / Block 6 里
> 读会被打断。这里只记一句：**只读工具永不过闸，写工具在唯一收口处被拦成"待确认"，
> 确认请求不带参数（token 是动作级凭证，参数只从存储快照读）**。细节按那份文档的 90 分钟路线走。

### Block 7 · 收口（30 min）

- 合上所有文档，白板画图（13 节点 + 两条降级链）；
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
- 必须同步改 `ToolSpec`（签名进 prompt）和至少一条测试；注意新工具若 risk!=read 会自动过沙箱闸门——顺手体验挂起；
- 跑 `mcp_probe` 不需要动 Java，但你要能说清「mock 和 MCP 同签名」是结构保证的。

### 实验 3 · 跑一轮故障演练并读报告（60 min）

```powershell
py -3.12 -m scripts.fault_drill --faults stop-qdrant,stop-redis --rounds 1 --run
```

> 不加 `--run` 是 dry-run（只打印计划）；默认 faults 里还有 stop-mysql，本项目没起 MySQL，务必显式指定。
> 演练会真的 `docker stop` 依赖，别在演示前跑。

- 读 `eval_output/fault_drill_*.json` 的 `definition` 字段（口径全在那）；
- 对照 Grafana：`降级应答`、`熔断状态`、`工具来源` 三个面板；
- 重点理解：**为什么 stop-qdrant 的可用性是 100%，而 stop-redis 恢复要 30 秒以上**。

### 下午剩余时间

- Block 4（缓存内部）40 min、Block 5（可观测）50 min、Block 6（韧性/MCP）50 min；
- 最后 30 min：录音复述 + 20 题自测。

**一天版时间账**：上午 3h + 实验 2h40 + 三个 block 2h20 ≈ **8 小时**。

---

## 三、20 道自测题（先自答，再对照答案）

| # | 问题 | 答案 |
|---|---|---|
| 1 | 13 个节点顺序？ | route → (chitchat↩) memory_load → cache_exact ──命中──▶ END；miss → embed → cache_semantic ──命中──▶ END；miss → (singleflight) → dispatch → tools∥recall → tools_done → generate / await_confirm。四个终局：chitchat、exact/semantic 命中（END）、generate、await_confirm |
| 2 | 为什么缓存拆成 exact / semantic 两段？ | exact 查询只是 Redis GET，命中**不付 BGE 编码**（旧图 stage p50 embed_query 6.33s，TTFT[hot]≈[cold]≈6.4–6.7s）；拆后 exact 命中 TTFT 21–26ms（真机）。编码只由 miss 请求支付，结果供语义缓存+向量召回共用 |
| 2 | 哪两个节点并行？为什么能并行？ | tools 与 recall（dispatch 分叉、tools_done 汇合）。无数据依赖：工具查实时数据、召回查知识库，hybrid 才两路都跑。并行收益 = min/max，主要价值是 hybrid 少付一倍墙钟 |
| 3 | 缓存命中走哪条边？ | `cache_exact` 命中 → END（`_after_cache_exact`）；exact miss → embed → `cache_semantic` 命中 → END。token 由 `_stream_cached_frames` **回放** Redis 里的完整答案（24 字符/片），不调 LLM——假流式 |
| 4 | meta 帧为什么在 cache_exact / cache_semantic 发？ | meta 的 `cache=exact/semantic/miss/similarity` 只有查完缓存才知道。exact 命中在 cache_exact 就终局（自己发 meta）；miss 路径的结论要等语义查询结束，meta 挪到 cache_semantic 统一发——保证它仍是第一帧且内容准确。cache_exact 的 miss 是**静默**的（一个帧都不发）；记忆降级由携带 meta 的节点补发。route 节点本身不发帧 |
| 5 | 路由四类与优先级？ | chitchat（整句寒暄且无实体）→ 写操作+知识=hybrid(0.86) → 写操作=tool(0.82) → 默认 rag；live+knowledge=hybrid(0.85) → live=tool(0.8)。实体优先于寒暄；默认 rag 是刻意的不对称（tool 误判漏知识=静默，rag 误判=用户重问可恢复） |
| 6 | 为什么规则路由可评估？ | `decide()` 纯函数、零 IO、可离线复现；同一份函数服务线上与评估，52 条标注反复跑出准确率/混淆矩阵。模型路由准确率随版本漂移且每次评估花钱 |
| 7 | 三层记忆载体？ | LangGraph state（本轮）→ Redis list 滑窗（会话历史）→ Redis hash + TTL（长期事实）。跨请求状态必须进共享存储：nginx 轮询下进程内存等于丢失 |
| 8 | Redis 挂了会怎样？ | 问答继续：load 返回空、save 返回错误串 → `degraded{stage:memory}` 由携带 meta 的节点补发。原则：锦上添花的依赖不能拖垮主链路，但降级必须可见 |
| 9 | 事实为什么规则抽取？ | 确定、零成本、可单测；抽错代价低（只是背景提示）。模型抽取每次请求多一次 LLM 调用且结果不稳定。抽取在**保存时**做——本轮问题模型本来就看得见，下一轮才生效 |
| 10 | 三级缓存防线？ | exact（键全等，零误伤）→ semantic（KNN + 护栏：实体不一致拒、阈值 0.96）→ singleflight（并发去重，leader 唯一算、follower 回放）。各解决：重复问、换着问、一起问 |
| 11 | singleflight 锁何时释放？ | `node_generate` 的 finally（`:1170`）——必须在 `cache.store` 之后（否则 follower 醒来仍是 miss），且覆盖正常/LLM 异常/客户端断开三条路径。**已知缺口**：纯写挂起路径（await_confirm）不经过 generate，锁靠 30s TTL 兜底 |
| 12 | kb_version 作用？ | 进缓存 key（`config.py:116`）；bump 后旧答案天然不命中，多副本一致（都读 Redis 里的运行时版本，不是启动快照）。运行时从 cache 读——settings 会过期 |
| 13 | 熔断三态？ | closed（正常，按滑动窗计失败率）→ open（快速失败，不碰下游）→ half_open（只放一个探测请求，成功才闭合）。关键在 open 时**连重试的机会都不给** |
| 14 | 重试白名单？ | 瞬态错误、超时、连接失败、5xx；4xx 与业务错误不重试（重试也不会成功，只放大流量）。退避带 jitter，防惊群 |
| 15 | 两种降级维度？ | 纵向：同工具多数据源（mcp→cache→mock，Hop 记录，`degraded_from` 进事件流）；横向：同意图多路径（skill 的 `previous_empty` fallback）。一个解决"数据从哪来"，一个解决"意图还能怎么达成" |
| 16 | `ToolRegistry.call` 契约？ | 永不抛，失败返回 `ToolResult(ok=False)`。上层是 SSE 流，抛了就断流；返回错误对象则降级链、degraded 帧、trace 记录全部照常走。挂起/拒绝不算失败（有 status 区分），不污染工具失败率 |
| 17 | 降级为什么必须可见？ | 否则 mock 数据被当成实时数据——答案"看起来正常"最危险。落实在三处：`tool_result.source` 字段、`degraded` 帧、Hop/trace。与"配置≠降级"同源：`rerank:disabled` 只发 notice 不发 degraded |
| 18 | 请求数用哪个指标？ | `chat_requests_total`（HTTP 层，含缓存短路 result=hit 与断连 aborted）。`llm_requests_total` 只是 LLM 调用数——命中率 75% 时它会让你误判"无流量" |
| 19 | 评估指标为什么 POST 进 api？ | prometheus_client registry 是进程内的，独立进程 inc 了没人抓。必须 POST `/admin/eval/metrics` 进 api 进程；gauge 重启清零 → 发布落盘、启动回填 |
| 20 | 项目最大短板？ | ① ~~embed 在缓存前~~ **已修（D4）**：拆成 cache_exact → embed → cache_semantic，exact 命中零编码（TTFT 21–26ms）；仍待修：② 慢故障无告警（错误率告警需要持续流量）；③ RAGAS 未正式跑；④ trace 多副本并发写损坏（682 行坏行，修法已定：O_APPEND 单次写）；⑤ 纯写挂起路径不释放单飞锁 |

---

## 四、读代码的方法（省一半时间）

1. **先读测试，再读实现**：`tests/test_intent.py`、`test_orchestrator_graph.py`、`test_memory.py`、`test_resilience.py`、`test_sandbox.py` 把这几个模块的契约写死了，比正文短得多。
2. **只读函数签名和 docstring**：本项目 docstring 写的是「为什么」，正文是「怎么做」；第一遍只要「为什么」。
3. **遇到长函数，只看 `return` 和 `raise`**：`node_recall`（80+ 行）实际只有 3 条出口。
4. **不要线性读 orchestrator.py 1317 行**：按 Block 2 的表格跳读，节点之间互相独立；**行号会漂移，按函数名搜**。
5. **每读完一块，立刻用一条命令验证**：能跑出来的理解才算数（selftest 的 `--only` 就是为此准备的）。
6. **把踩过的坑当记忆锚点**：`rerank:disabled` 假降级、`llm_requests_total` 误当请求数、nginx 只解析一次、uid 不匹配、多副本 trace 写坏——这五个故事比任何概念都好记，而且面试官爱听。

---

## 五、时间不合理的判断

- **半天足够**「能讲、能答常规追问」，但不足以覆盖缓存内部实现和 Java MCP 细节；
- **一天是舒适区**：能加三个实验，面试时可以说「我改过阈值/加过工具/跑过演练」，可信度完全不同；
- **不建议超过一天**：项目细节的边际收益很快递减，剩下的时间应该拿去练讲解（录音 3 遍比多读 300 行有用）。

**最低配版本（只有 3 小时）**：Block 0 + Block 2 + Block 3 + 背 20 题。
这三块覆盖了面试 80% 的追问（编排、记忆、请求生命周期）。
