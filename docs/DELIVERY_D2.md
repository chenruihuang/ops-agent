# D2 交付与包装（面试版）

> 用途：验收 D2 成果、写简历、准备讲解与演示。
> 所有数字都来自本仓库的真实产物，括号里标了口径与出处；凡标「不可引用」的，不要写进简历。

---

## 一、交付清单（可验收）

| 交付物 | 位置 | 验收方式 |
|---|---|---|
| 可运行系统 | `docker-compose.yml`（api×2 / nginx / redis / qdrant / prometheus / grafana / java-mcp） | `docker compose ps` 全 healthy；`curl localhost:8080/health` |
| 自动化测试 | `tests/`（20 个 check 文件） | `py -3.12 -m pytest tests -q` → **193 passed · 689 条断言** |
| 端到端自测 | `scripts/selftest.py`（9 个场景） | `python -m scripts.selftest` → 全 PASS |
| MCP 协议自检 | `scripts/mcp_probe.py` | 19/19（默认容器路径 + `--url http://127.0.0.1:18081/mcp`） |
| 答案侧评估 | `scripts/run_eval.py` + `eval_output/answers_full.json` | `--rescore` 可离线复算，不调模型 |
| 故障演练 | `scripts/fault_drill.py` + `eval_output/fault_drill_*.json` | 5 轮×2 故障，JSON/Markdown 报告 |
| 监控大盘 | `monitoring/grafana/build_dashboard.py` → 31 个面板/行 |
| 安全沙箱 | `app/tools/sandbox.py` + `POST /chat/confirm` + `GET /admin/sandbox/audit` | `pytest tests/test_sandbox.py -q -s`（16 passed / 79 断言）；手工一轮见 `SELFTEST.md` 阶段 10 | `http://localhost:3001`（admin/admin123），顶行 ⓪ 有数 |
| 文档 | `README.md`（架构/取舍/口径）、`SELFTEST.md`（10 阶段自测+排障表） | 按目录走一遍 |

**一条命令的最终验证**：

```powershell
cd D:\ai-project\ops-agent
py -3.12 -m pytest tests -q                                  # 193 passed / 689 断言
python -m scripts.mcp_probe                                  # 19/19
python -m scripts.selftest                                   # 端到端全 PASS
```

---

## 二、简历写法

### 一句话版本

> 基于 LangGraph 构建企业级运维知识 Agent：多知识库 RAG + 实时工具调用 + 两级缓存 + 熔断/重试/降级，配 Prometheus/Grafana 可观测与故障注入验收。

### 项目条目（可直接粘贴，按岗位删减）

**企业级运维知识 Agent 平台**（个人项目 · 2026.09–2026.10）

- 设计 **LangGraph 10 节点状态机**编排「路由 → 记忆 → 缓存 → 单飞 → 工具∥召回并行 → 生成」，SSE 逐帧事件协议（meta/route/tool_call/docs/token/notice/degraded/done），缓存命中路径同样可观测。
- 实现 **4 类意图规则路由**（rag / tool / hybrid / chitchat）：实体优先于寒暄、实时信号与知识信号可叠加为 hybrid；路由是零依赖纯函数，可单测、可离线评估。
- RAG 检索采用 **BM25 + 向量双路召回 + RRF 融合**（检索层 recall@k 0.6421，单路 0.6029 / 0.6075；重排行 2026-10-24 已复测：RRF+重排 strict 0.6029 / MRR 0.7518 / hit 0.9617——重排买名次不买覆盖，线上关闭，见 `matrix_qdrant_rerank_20261024-170116.json`）；答案侧自建 187 题标注集，**context recall 0.6184 / 文档级 AP 0.4774 / 关键事实命中 0.8538**（口径见 README，非 RAGAS；2026-10-23 语料扩容至 1010 文档 / 3784 点后的新口径）。
- 缓存三层防线：**exact 命中 → 语义 KNN 命中（带护栏）→ singleflight 合并并发未命中**；阶梯压测（10→120 并发，1804 请求）未命中并发只打一次 LLM、**降级仅 1 次真实故障**；另一轮 287 请求的分布中综合命中率 **74.9%**（exact 73.2% / semantic 0.3%）。
- 韧性设计：**熔断（外）→ 重试（内，只重试瞬态/超时/5xx）**，纵向降级链 `mcp → cache → mock`，横向按 Skill 声明式 fallback；故障注入 10 轮：**stop-qdrant 可用性 100%、告警检测 0.55s、MTTR 27.95s**（MTTR=T3−T2，测量条件见报告）。
- 可观测：**18 个自定义 Prometheus 指标 + 31 面板 Grafana 大盘 + JSONL/Langfuse 双后端 trace**；为「指标名写错不报错只画空线」写守门测试（指标存在 / 有写入点 / 面板有 noValue）。
- 工具接入：**Java（Spring Boot）实现 MCP Server**，5 个业务工具 + `X-Chaos` 故障注入头 + Actuator 指标；Python 侧 MCP 客户端统一协议，19 条协议自检覆盖 initialize / tools/list / tools/call / chaos 三态。
- 安全沙箱：工具按风险分级（read / write / dangerous），写操作走 **参数白名单 → 一次性凭据人工确认 → 审计**；确认执行时参数**只从服务端待办快照读取**（请求体不含 tool/args），凭据用 Redis `GETDEL` 原子消费、10 分钟过期；纯写挂起的一轮不生成答案（SSE `confirm_required` + `done.status=awaiting_confirmation`），由 `/chat/confirm` 续跑，多副本/重启/断连下不丢；**复合诉求（"重启 X，并查 OOM 怎么办"）先答知识部分、同一帧里带 `status=awaiting_confirmation`**，写操作照样确认，该轮不写答案缓存。

### 技术栈行

Python 3.12 / FastAPI / LangGraph / Redis(RediSearch) / Qdrant / BM25 / RRF / Prometheus / Grafana / Locust / Docker Compose / Java 17 + Spring Boot + Micrometer / MCP

### 不要写进简历的四句话

1. **不要写「可用性 99.9% / SLA」**——项目里只有「故障注入下的请求成功率」，口径写在 `fault_drill` 报告的 `definition` 字段里。
2. **不要写绝对延迟**（如「P95 3s」）——共享 vCPU 上同一标尺实测漂 7.5 倍，只报同轮相对差异。
3. **不要把 `llm_requests_total` 当请求数**——它是 LLM 调用数，缓存命中不计；请求数看 `chat_requests_total`。
4. **不要写「RAGAS 已跑」**——当前是确定性近似指标（recall/AP/anchor），RAGAS 是否落地要如实说「未接入」。
5. **不要把写工具说成「真重启」**——`restart_service` / `scale_service` 是 `simulated=true` 的模拟执行，返回文本和事件里都写明了；说成真的，面试官问一句「调的是哪个 API」就穿帮。

---

## 三、讲解脚本

### 3 分钟版（电梯陈述，按「问题 → 方案 → 证据 → 权衡」）

**0:00–0:30 问题**
「运维同学查一个工单进展，要在知识库、工单系统、资产系统之间来回切。我要做的是一个入口：能答知识（SOP/手册），也能查实时数据（工单/告警），并且**答不出来时要说得清是哪里退化**。」

**0:30–1:30 方案（只讲一条请求怎么走）**
「一条请求进来是 LangGraph 的 10 节点状态机：先规则路由分四类——知识、实时、混合、闲聊；混合会 fan-out，工具和召回**并行**跑。中间有两级缓存和 singleflight：命中直接回放 SSE；没命中才走完整链路，并发未命中只让一个请求去打 LLM。工具侧是 Java 的 MCP Server，外面套熔断→重试→降级链。」

**1:30–2:30 证据**
「三组数：检索层 RRF 0.6421 高于单路；答案侧 187 题 recall 0.6184；故障注入停掉 Qdrant，可用性 100%、检测 0.55 秒、MTTR 28 秒——因为向量挂了还有 BM25，降级是设计出来的，不是运气。」

**2:30–3:00 权衡（这句最加分）**
「我知道它的短板：embed 排在缓存查询之前，exact 命中也要付一次 BGE 编码，所以 hot/cold 的 TTFT 几乎一样；stop-redis 那次 162 秒才恢复，因为延迟型故障没触发告警，只靠超时兜底。这两个都在 README 的「已知瓶颈」里，下一步分别是拆节点和补慢故障告警。」

### 10 分钟版（配界面/命令，按演示顺序）

| 时间 | 讲什么 | 手上做什么 |
|---|---|---|
| 0–1′ | 架构图：入口 nginx → 无状态 api×2 → Redis/Qdrant/Java MCP；监控旁路 | 指 README 架构图 |
| 1–3′ | 路由：实体优先于寒暄；hybrid 两路并行 | 问一句「你好，INC-2026-0412 到哪了」，看 route 事件 |
| 3–5′ | 缓存与单飞：exact→semantic→singleflight | 同一问题再问一次，看 `cache=exact` 和 `tokens_saved` |
| 5–7′ | 工具接入：MCP 协议、工具来源（mcp/mock）、Skill 横向降级 | 问「SRV-0088 现在有什么告警」，看 tool_call 帧 |
| 7–9′ | 高可用：停 java-mcp 演示降级；SSE degraded + breaker=open；恢复 | `docker stop ops-agent-java-mcp-1` |
| 9–10′ | 可观测与评估：大盘、trace 一条请求的分段耗时、评估口径 | 打开 Grafana + 一份 trace JSONL |

> 讲解时**主动说口径**：recall 是自建标注集的文档级指标，不是 RAGAS；MTTR 是 T3−T2 且依赖告警检测。
> 面试官最认这种「知道自己数字怎么来的」的候选人。

---

## 四、Demo Runbook（演示前 10 分钟 + 5 步演示）

### 演示前检查（5 条，逐条打勾）

```powershell
cd D:\ai-project\ops-agent
docker compose ps                                   # 全部 Up/healthy
curl.exe -s --noproxy '*' http://127.0.0.1:8080/health   # {"status":"ok",...}
curl.exe -s --noproxy '*' http://127.0.0.1:18081/health  # java-mcp: 带 "tools":5
python -m scripts.mcp_probe                         # 19/19
python -m scripts.sandbox_demo --auto                # 四幕全绿（末尾「失败检查 0 项」）
```

浏览器：Grafana `http://localhost:3001` → 大盘时间窗 **Last 1 hour**，确认顶行 ⓪ 有数（没有就先发一条请求）。

### 演示六步（每步都有「看点」，避免只看到界面在动）

1. **知识题**：问「连接池耗尽的处置步骤是什么」
   看点：`route=rag`、`docs` 帧带来源、token 流式、`done` 收尾。
2. **实时题**：问「INC-2026-0412 处理到哪了」
   看点：`tool_call` → `tool_result`，`source=mcp`；结果不是知识库里的。
3. **缓存**：原样再问一次
   看点：`cache=exact`、`similarity`、`tokens_saved`；回答秒回。
4. **降级**：`docker stop ops-agent-java-mcp-1`，再问实时题
   看点：`degraded{stage:tool, reason:...}`、`source=mock`、`breaker=open`；**答案仍然给出，但明确标注降级**。
5. **恢复**：`docker compose start java-mcp`，等 `/health` 带 `tools`，再问一次
   看点：`source=mcp` 回来；Grafana 的 `工具结果来源`、`熔断状态`、`降级应答` 面板同步变化。
6. **安全沙箱**：`python -m scripts.sandbox_demo`（或 `--auto` 四幕连跑）
   看点：问「帮我重启 order-service」→ `tool_result(status=pending_confirmation)` →
   `confirm_required{risk=dangerous, expires_in=600}` → `done{status:awaiting_confirmation}`
   **本轮不生成答案**；按 `y` 后 `status=executed` + `simulated=true`；按 `r` 用同一 token 重放 → 404。
   再问「帮我重启 payment-gateway」→ 策略拒绝、**没有审批单**、正文照常生成（说明不是"凡写必挂"）。
   再问「帮我重启order-service,并查一下oom怎么办」→ `route=hybrid`、
   `restart_service` 挂起、**OOM 那一段照常答**，`done{status:awaiting_confirmation, route:hybrid}`
   （这一句最能说明"漏判写操作"和"吞掉知识答案"是两种相反的错误，都不接受）。
   收尾 `/audit` 看 `pending / approved / rejected / denied` 四类事件。

### 演示后

```powershell
docker compose start java-mcp        # 确保恢复
python -m scripts.mcp_probe          # 19/19 再确认一次
```

> 注意：停依赖会影响正在进行的请求；演示时选在没人用的时候做。
> 评估指标是进程内 gauge，重启后由 `data/traces/eval_metrics.json` 自动回填，不用重跑评估。

---

## 五、高频追问速答（13 条，背骨架即可）

| 追问 | 回答骨架 |
|---|---|
| 为什么用 LangGraph，不用函数串起来？ | 链路有 fan-out（tools∥recall）、有 4 个终局、有跨节点状态；状态机让「并行 + 条件边 + 单飞回放」显式化，且 trace 的 span 边界能和节点一一对应。 |
| 意图路由为什么用规则不用模型？ | 纯函数零依赖、可单测、可离线评估；误判代价不对称（偏向 rag 更安全）；代价是泛化差，已在 README 写明，模型路由是下一步。 |
| 缓存一致性怎么保证？ | key 带 `kb_version`；`/admin/kb/bump` 递增后全副本立即失效；语义缓存有护栏（相似度阈值 + 路由/意图校验）。 |
| singleflight  follower 怎么处理？ | 抢不到锁就 `wait_for_result`，领跑者写缓存后 follower 直接回放同一答案；锁释放放在 `node_generate` 的 finally，成败都释放。 |
| 熔断和重试为什么这个顺序？ | 重试在内、熔断在外：重试放大流量，熔断先挡住持续故障；只重试瞬态/超时/5xx，永久错误不浪费配额。 |
| 降级链路的「真实」怎么证明？ | SSE 里 `source=mock`/`degraded_from`、`breaker=open`，指标 `tool_calls_total{source}`、`degraded_responses_total`；停掉 java-mcp 实测一条请求即可复现。 |
| 记忆存在哪，为什么不用进程内？ | ①工作记忆=LangGraph state，请求结束即丢；②会话=Redis list 滑窗；③长期事实=Redis hash。进程内记忆会让横向扩展失效。 |
| 指标口径踩过什么坑？ | `llm_requests_total` 当请求数（缓存命中不计）→ 换成 `chat_requests_total`；0/0 被 clamp 成 0% → 改 noValue；配置关闭被当降级 → 改 notice。 |
| 数据怎么评估的？ | 187 题标注集（183 题有 golden），答案侧：context recall、文档级 AP、关键事实命中率、弃答率；脚本 `--rescore` 可离线复算。 |
| 项目短板？ | ①embed 在缓存查询前，hot≈cold；②延迟型故障无告警，stop-redis 靠超时兜底 162s；③没接 RAGAS/LLM-as-judge。 |
| 写操作怎么防止模型乱执行？ | 工具按 `risk` 分级，read 之外一律过沙箱：参数白名单（值白名单优先于正则，`payment-gateway` 重启直接禁）→ 挂起发一次性 token → 确认执行。**确认接口不收 args**，要执行什么从服务端快照读 —— 这是防「确认 A 执行 B」的关键。 |
| 确认后「继续执行」怎么做的？ | 落库 + 新请求续跑（`POST /chat/confirm`），不是挂住 SSE。理由：挂 10 分钟会被代理掐断、进程重启全丢、多副本对不上。也说明为什么没用 `interrupt()`：它依赖 checkpointer，本项目还是 backlog。 |
| 沙箱的失效模式是什么？ | 静默型：写工具的 `risk` 漏标成 read → 每次执行都不弹确认，而且**不报错**。所以上了 `sandbox_decisions_total` 面板：有写工具却没有 `pending` 计数 = 闸门没接。 |

---

## 六、口径红牌（讲错一句，前面全白讲）

1. **可用性** = 故障注入期间探测请求成功率，不是 SLA。
2. **MTTR** = T3−T2（检测到 → 恢复），必须同时报检测延迟和测量条件。
3. **检索 recall 0.6421** 是检索层指标，**答案 recall 0.6184** 是答案侧指标，不要混用。
4. **stop-redis 162s** 要主动讲成「慢故障缺告警」的待办，不要藏。
5. **命中率 74.9%** 要说明是「那轮压测的分布」，不是永久能力。
6. **写工具是 `simulated=true`**，返回里写着「未真正调用编排接口」——不要讲成真重启了。
7. **挂起（pending）不算降级**，也不计入失败率；`denied`（策略拒绝）和 `rejected`（人拒绝）是两件事。
