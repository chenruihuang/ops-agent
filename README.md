# 企业智能运维 Agent 平台

> 面向 IT 运维场景的多模态知识问答与工单自动化 Agent。
> 私有化一键部署、多路召回 + 精排、两级语义缓存、全链路可观测。
> 所有性能数字均来自本地可复现实验，测量条件随指标一并标注。

**交付与学习材料**：`docs/DELIVERY_D2.md`（D2 验收清单 / 简历条目 / 3 分钟与 10 分钟讲解稿 / Demo Runbook / 高频追答）、
`docs/STUDY_PLAN.md`（半天到一天的项目熟悉路线，按代码行号给读法）、
`docs/STUDY_SANDBOX.md`（安全沙箱 90 分钟精读路线：闸门 / 凭据 / 图改道 / 12 道自测题）。

---

## 1. 为什么是这个场景

转行求职时，个人项目最大的风险是"什么都做了，但讲不清为什么"。
所以这里只讲**一条业务故事**，把所有技术点挂在这条故事上：

| 运维场景的天然难点 | 对应的技术能力 | 为什么面试官会认可 |
|---|---|---|
| 排障手册是 PDF/Word，报错截图是图片 | 多模态知识库（文本 + 图像） | 双模态是真需求，不是为了凑数 |
| 同一个问题，答案可能在手册、历史工单、资产台账里 | **双路**召回（向量 / BM25）+ 工具层 | 能讲清"哪类数据**不该**进向量库" |
| "SRV-0001 现在什么状态"是实时数据 | MCP 工具调用，而不是 RAG | 区分 RAG 与工具调用是分水岭 |
| 高频问题反复被问（VPN、密码、账号） | 两级语义缓存 | 直接对应降本增效，客户最关心 |
| 客户环境里向量库/LLM 都会挂 | 熔断、重试、降级链 | Java 工程经验的直接迁移 |
| 客户一定会问"为什么答错了" | 自建标注集 + 分层归因 | 绝大多数候选人没有这一环 |

**统一叙事（简历用）**：从单文档 RAG 服务（cs-agent，Hit@5 100%、MRR 0.8417）
迭代为企业级多知识库 Agent 平台，补齐多路召回、语义缓存、工具接入与可观测体系。

---

## 2. 架构

```mermaid
flowchart TB
    subgraph L0["接入层"]
        NX["nginx 负载均衡<br/>SSE 关闭缓冲"]
        GW["鉴权 · 并发闸门 · 幂等"]
    end

    subgraph L1["编排层"]
        RT["意图识别路由<br/>rag / tool / hybrid"]
        LG["LangGraph 状态机"]
        DG["降级链"]
    end

    subgraph L2["能力层"]
        RC["多路召回"]
        RK["Rerank 精排"]
        SK["Skill 注册表"]
        MC["MCP Client"]
    end

    subgraph L3["缓存层 ★"]
        E1["L1 精确缓存<br/>归一化 + 版本号"]
        E2["L2 语义缓存<br/>KNN + 锚点护栏"]
        E3["子链路缓存<br/>改写/重排/工具结果"]
    end

    subgraph L4["记忆层"]
        M1["工作记忆<br/>LangGraph state"]
        M2["会话记忆<br/>Redis"]
        M3["长期记忆<br/>向量库"]
    end

    subgraph L5["数据与外部系统"]
        QD[("Qdrant<br/>多模态向量")]
        BM["BM25 索引"]
        SQL[("MySQL<br/>工单/资产/用户")]
        JV["Java MCP Server"]
    end

    subgraph L6["贯穿层 · 可观测与评估"]
        LF["Langfuse Trace"]
        RG["RAGAS 评估"]
        PM["Prometheus + Grafana"]
        LC["Locust 压测"]
    end

    NX --> GW --> RT
    RT --> LG
    LG --> E1
    E1 --> E2
    E1 -.命中.-> OUT
    E2 -.命中.-> OUT
    LG --> RC
    RC --> QD
    RC --> BM
    RC --> MC
    MC --> JV
    JV --> SQL
    RC --> RK --> LG
    LG --> E3
    LG --> M1
    LG --> M2
    LG --> M3
    LG --> OUT["SSE 流式输出"]

    L3 -.-> L6
    L2 -.-> L6
    L4 -.-> L6
```

---

## 3. 快速开始

### 3.1 全栈启动（需要 Docker）

```powershell
cd D:\ai-project\ops-agent

Copy-Item .env.example .env      # 填 DEEPSEEK_API_KEY

.\scripts\dev.ps1 build          # 构建并启动全栈
.\scripts\dev.ps1 kb             # 建知识库（入库 + 版本号推进）
.\scripts\dev.ps1 urls           # 查看所有服务地址
```

### 3.2 只验证入库链路（不需要 Docker、不需要模型）

语料的切块、建索引、建图谱、召回率评估这些"逻辑正确性"部分，
**不依赖任何外部服务**，可以单独跑：

```powershell
python -m scripts.gen_image_transcripts     # 截图 → OCR 转录（离线回放用）
python -m scripts.ingest --stage build      # 切块 + 建点 + BM25 + 图谱（秒级）

python -m scripts.eval_retrieval --backend bm25 --sweep   # 关键词路召回率（真实数字）
python -m scripts.check_intent --min-accuracy 0.94        # 意图路由准确率（可进 CI 卡阈值）
python -m tests.test_chunker                # 41 项
python -m tests.test_indexing               # 76 项
python -m pytest tests/                     # 全部 193 个测试函数
```

> 这一步的意义：切块策略调一次就要重跑一轮评估，如果每轮都要等模型加载 + 容器起停，
> 一天改不了几次。把纯逻辑与外部依赖拆开，是能真正迭代的前提。

### 3.2.1 看一条流式回答（带 session_id）

```powershell
python -m scripts.stream_chat "连接池耗尽的处置步骤是什么" --session demo-01 --no-cache
python -m scripts.stream_chat "那 inode 耗尽呢" --session demo-01   # 同一会话，history_turns 应为 1
```

token 直接连成正文打在终端，`meta/route/docs/degraded` 压成一行摘要；
`--show-frames` 逐帧打印原始 JSON（排障用），`--no-cache` 强制走完整链路。

**不需要向量库在线的召回对比**（`--vector-source local` 走 build 落盘的 `vectors.bin`
做暴力扫描，与走向量库是同一段代码）：

```powershell
python -m scripts.eval_matrix --vector-source local      # BM25 / 向量 / 融合 三行
python -m scripts.bench_rerank --from-recall 20         # 重排代价：先实测候选池，再分层测延迟
```

> `--from-recall` 不是可选项。不带上它，脚本就只会用你手填的 `--pairs` 换算单题耗时，
> 而真实候选池是算出来的（默认配置下中位 **29 块**，不是 20）——
> 详见「重排的代价」一节。

| 服务 | 地址 |
|---|---|
| API 文档 | http://localhost:8080/docs |
| Qdrant 控制台 | http://localhost:6333/dashboard |
| Grafana | http://localhost:3001 （admin / admin123） |
| Prometheus | http://localhost:9091 （宿主 9091 → 容器 9090，见下方端口规划） |
| Locust 压测 | http://localhost:8089 （需先 `dev.ps1 load`） |

**宿主端口规划**（本机同时跑 cs-agent，端口必须错开）：

| 用途 | 本项目 | 冲突方 | 备注 |
|---|---|---|---|
| 统一入口 | 8080 | cs-agent 8000 | nginx |
| MySQL | 3307 | 本机 MySQL 3306 | 容器内仍是 3306 |
| Grafana | 3001 | cs-agent 3000 | |
| Prometheus | **9091** | **cs-agent 9090** | 容器内仍是 9090，Grafana 数据源不受影响 |

> ⚠️ 端口冲突的表现很有迷惑性：`docker compose up -d` 会因**某个**容器
> `port is already allocated` 而**中止整批启动**，其余容器停在 `Created` 状态
> （`docker ps -a` 里状态是 `Created`、退出码 0，看起来"没报错"，实际一个都没起）。
> 所以 `docker compose ps` 里出现 `created` 时，先查 `docker inspect <容器> --format '{{.State.Error}}'`。

---

## 4. 当前实现状态

**诚实标注**很重要 —— 下面的"已验证"是真跑过测试的，不是"看起来对"。

| 模块 | 状态 | 说明 |
|---|---|---|
| 缓存逻辑（keys / guards） | ✅ 已验证 | 17 项单元测试通过，含中文数字归一化、锚点护栏 |
| 缓存管理器（两级 + 单飞） | ✅ 已验证 | 19 项测试通过（用注入式假 Redis，含暴力 KNN） |
| **语料解析 + 分块** | ✅ **已验证** | 110 篇 → 391 块（另 20 张截图 → 77 块）；48 项测试通过 |
| **向量存储抽象层** | ✅ **已验证** | Qdrant / Chroma / Local 三后端同一契约，31 项测试通过 |
| **多模态入库（OCR + 描述）** | ✅ **已验证** | 20 张 → 77 块；32 项测试通过（离线回放 provider） |
| **索引层（点 + BM25 + 图谱）** | ✅ **已验证** | 468 点 / 10921 词项 / 447 边；61 项测试通过（含向量缓存复用判据） |
| **向量召回接入（多路 + 融合）** | ✅ **已验证** | 真跑过：Qdrant 468 点，向量 0.875 / 融合 0.8958（见 §6.4） |
| **CrossEncoder 重排** | ✅ **已验证（代价与收益都实测）** | 全套 48 题 0.8958→0.9201；但**线上口径只 +0.69 pp（低于分辨率）**；代价 64.7 s/题 → 已设 `RERANK_ENABLED` 开关 |
| **意图路由** | ✅ **已验证** | 标注集 52/52 · 手写对抗集 19/19（见「意图路由」一节） |
| **工具层（MCP + mock 降级）** | ✅ **已验证** | 5 个工具 · `call()` 永不抛异常 · 45 项测试通过（见「工具层」一节） |
| **SSE 端到端** | ✅ **已验证** | 事件顺序/降级/缓存命中全部有断言，59 项测试通过（见「SSE 事件协议」一节） |
| **召回率评估口径** | ✅ **已验证** | 评估与服务端**共用同一条召回链路**（见 §6.4 的收敛说明） |
| 全栈编排（compose / nginx / Prometheus） | ✅ 已就绪 | 配置文件完整，未在真实 Docker 上端到端跑通 |
| **三层记忆（工作 / 会话 / 长期事实）** | ✅ **已实现，测试覆盖** | 会话滑窗+事实外置 Redis（进程零会话状态）；记忆故障降级可见 `stage=memory`；吞吐数字待压测 |
| **LangGraph 编排** | ✅ **已实现，SSE 协议逐帧对齐** | 10 节点状态机（路由/记忆/缓存/单飞/工具∥召回并行/生成），事件协议一字未改，端点级用例全部原样通过（见「LangGraph 编排与三层记忆」） |
| **全链路可观测** | ✅ **本地 + 云端均已验证** | 本地 JSONL 全量 + Langfuse 上报；10 个阶段 span、TTFT 三处同源、`trace_replay` 会话回放；`selftest --only trace` 16/16；真 key 下 `--send-test` + v2 API 核对到 12 条 observation（10 阶段 span 全上云，类型映射正确）。UI 截图未留（验证走 API），见 §6.x |
| **Java MCP Server（Spring Boot 3）** | ✅ **已实现，协议验证** | `java-mcp/`：JSON-RPC 2.0 三方法（initialize / tools/list / tools/call）+ 5 个工具与 Python 侧 `TOOL_SPECS` 一致 + X-Chaos 故障注入；chaos 三态（500×2 后恢复 / hang / sleep）实测通过 |
| **熔断 / 重试 / 降级链 + Skill 注册表** | ✅ **已验证** | `resilience.py`（三态熔断 + 白名单重试 + 显式降级链）+ `skills.py`（横向降级）；89 项断言；三场景实测：正常 / 抖动救回（attempts=3）/ 持续故障熔断开→mock 兜底 |
| **安全沙箱（写操作人工确认）** | ✅ **已验证** | 风险分级 + 参数白名单 + 一次性凭据（Redis `GETDEL`）+ 审计日志；写工具 2 个（`restart_service` / `scale_service`，`simulated=true`）；`/chat/confirm` 续跑；84 项断言，见 §6.aa |
| 压测脚本（TTFT） | ✅ 已就绪 | Locust 分段 10→120 用户 / 6 分钟，事件流级 TTFT；**本轮不引用绝对延迟**（共享 vCPU） |
| **故障注入 / MTTR 测量** | ✅ **已验证（真跑过）** | `fault_drill.py` 多故障 × 多轮；实测 stop-redis：检测 19.4s、可用性 33.3%、请求耗时中位 31.8s（基线 ~4s）。修正了三处测量 bug，见 §6.z.3 |
| **答案侧评估（RAGAS 口径）** | ✅ **已实测** | `run_eval.py`：recall 0.8785 / precision 0.7841 / anchor 0.8237（48 题有 golden） |
| **Grafana 大盘** | ✅ 已生成，待人工目检 | 31 面板（含安全沙箱一行）；生成脚本 + 指标名守门测试（指标写错时 Grafana 只会画空线） |
| **Java MCP 指标** | ✅ 已验证 | Actuator + Micrometer：`mcp_tool_calls_total` / `mcp_tool_seconds` / `mcp_chaos_total`，`/actuator/prometheus` 实测 200 |

> 测试口径：`pytest tests/` → **193 个测试函数全部通过**；
> 另外 **689 条 `check()` 型断言**（覆盖 20 个文件）已纳入 pytest 退出码。
> 全部离线，不需 Docker / 模型。
> 异步用例不需要 `pytest-asyncio` 也能跑（`tests/conftest.py` 兜底），装了则由
> `pytest.ini` 的 `asyncio_mode = auto` 接管 —— 两条路径都验证过。
>
> ⚠️ **一个已修复的测试盲区**：早期有一批用例用 `check("名字", 条件)` 累加
> 模块级 `PASS/FAIL`，但 pytest 只看 `test_*` 函数，**没人去读 `FAIL`** ——
> 于是断言全红时 pytest 依然报绿。也就是说"用例通过"这个数字里，
> 占比最大的那部分断言当时没被 gate 住。现在 `tests/conftest.py` 在
> session 结束汇总各模块的 `FAIL` 并置退出码非 0，这个盲区已经关掉。
> 关掉的当天它就抓出一条**已过期 3 个月**的断言（标注集从 40 条扩到 52 条，
> 但测试里还写死着 `len(report.scored) == 36`）。

---

## 5. 缓存设计（本项目重点）

### 5.1 为什么需要两级

| 层级 | 匹配方式 | 命中场景 | 实现 |
|---|---|---|---|
| L1 精确 | 归一化后完全一致 | FAQ、固定流程，企业场景占比 30%+ | Redis String |
| L2 语义 | query 向量 KNN ≥ 0.96 | "奖金怎么算" vs "奖金如何计算" | Redis Stack + RediSearch |
| L3 子链路 | 改写 / 重排 / 工具结果 | 动态数据，短 TTL | Redis String |

**缓存查询必须早于任何检索动作**。把缓存放在生成前只能省 LLM 那一段，TTFT 收益直接砍半。

### 5.2 时效性 vs 响应速度：这是两个相反的指标

| 指标 | 缓存的作用 | 本项目的处理 |
|---|---|---|
| 响应时效性（latency） | ✅ 大幅提升 | 命中时 TTFT 从秒级降到毫秒级 |
| 数据时效性（freshness） | ❌ 会损害 | 版本号强失效 + 分级 TTL |

**分级 TTL 策略**（`app/cache/manager.py`）：

- 稳定知识（手册、制度）→ 24h
- 语义缓存 → 6h（误命中代价更高，所以更短）
- 动态数据（工单状态、资产）→ 30s，或干脆不缓存
- 空结果 → 60s（防穿透）

### 5.3 版本号强失效：不删缓存的失效

```python
# 知识库更新后只需调用一次
await cache.bump_kb_version()      # 版本号 +1
```

所有 key 里都带 `kb_version`，版本号一变旧 key 自然无人去读，**不需要 SCAN + 批量 DEL**，
也不会在删除过程中产生脏读。

版本号存在 Redis 而不是进程内存里 —— 多副本场景下，如果只存在本地内存，
A 副本自增了 B 副本不知道，会出现"一半新数据一半旧缓存"的脏读。

演示用接口：
```powershell
.\scripts\dev.ps1 bump      # 版本号 +1，全副本缓存即时失效
.\scripts\dev.ps1 stats     # 查看当前版本号与内存占用
```

### 5.4 语义缓存的护栏：向量像 ≠ 语义等价

这是语义缓存**最危险的失效模式**，也是这个项目里最值得写进简历的一个细节：

> "16 岁能独立签合同吗" 与 "18 岁能独立签合同吗"
> 余弦相似度常年 0.97+，但法律后果完全相反。

所以命中前必须过两道护栏（`app/cache/guards.py`）：

1. **数字锚点一致性**：抽取问题中的数字/量词组合（含中文数字归一化，"十六周岁" → "16"），
   两边必须完全一致。一方有、一方没有也拒绝。
2. **长度比**：差距超 3 倍视为意图已变。

保守优先是刻意的取舍：**宁可多打一次 LLM，也不能让用户按错条款办事**。
代价是损失一部分潜在命中率，这个取舍本身在面试里就是加分项。

### 5.5 单飞（singleflight）—— 以及为什么没有无脑上

同题并发时只放一个请求进后端，其余等它的结果。**但要诚实说明它的代价**：

- 收益：保护后端、省 token、压测时错率好看
- 代价：**跟随者的延迟等于领跑者的完整生成时间**，可能比各算各的更慢

所以本项目把它做成可开关的（`CACHE_SINGLEFLIGHT_ENABLED`），并设置跟随者等待上限
（`CACHE_SINGLEFLIGHT_WAIT`，超时自己算）。这个"什么时候不该用"的回答，
比"我用了 singleflight"有价值得多。

### 5.6 其他防护

| 风险 | 手段 | 位置 |
|---|---|---|
| 击穿 | singleflight 互斥锁 | `acquire_singleflight` |
| 雪崩 | TTL 随机抖动 ±10% | `_ttl()` |
| 穿透 | 空结果短 TTL 缓存 | `set_exact` 按答案是否为空选 TTL |
| 锁名不一致 | 用 SHA1 而非内置 `hash()` | `lock_key()`，内置 hash 受 PYTHONHASHSEED 影响 |

### 5.7 缓存相关指标

`cache_requests_total{layer, result}` 的 `result` 有 `hit` / `miss` / `rejected` / `error` / `stale` 五种 ——
把 `rejected`（护栏拦截）单独统计，才能看出阈值调得对不对。

---

## 6. 五大指标的测量口径

> 每个数字都必须能说出测量条件。这一节是简历里所有数字的来源。

### 系统可用性 → 重新定义为「故障注入下的请求成功率」

```powershell
.\scripts\dev.ps1 fault stop-qdrant   # 断掉向量库
.\scripts\dev.ps1 fault stop-redis    # 断掉缓存
```

对比实验：**开启降级链 vs 关闭降级链各测一轮**，只报前者没有说服力。

写入简历的形态：
> 注入 3 类依赖故障（向量库断连 / 缓存不可用 / 外部系统超时），
> 降级链生效下请求成功率保持 100%（召回失败时降级为无资料应答），
> 关闭降级时跌至 XX%。

### TTFT

```powershell
.\scripts\dev.ps1 load-ci
```
Locust 报告里会同时出现 `SSE/chat/stream`（端到端）与 `TTFT/first_token`（首 token）两行。
**TTFT 必须解析 SSE 事件流来测**，普通压测工具的"首字节"测的是 `meta` 事件，不是 token。

### 并发数

阶梯加压 10 → 30 → 50 → 80 → 120，判定口径固定为：
**错误率 < 1% 且 TTFT P95 < 3000ms 时能撑住的最大用户数。**

横向扩展证明：
```powershell
.\scripts\dev.ps1 sky            # 扩展到 3 副本
# 同样跑一轮压测，对比吞吐
```

### 上下文召回率

```powershell
# 主入口：一个进程跑完整对比矩阵（模型只加载一次），并做两条向量路径的一致性校验
python -m scripts.eval_matrix --vector-source qdrant --parity
python -m scripts.eval_matrix --vector-source qdrant --rerank --only RRF   # 只跑 RRF 那一行 ±重排
python -m scripts.eval_matrix --vector-source local                        # 不需要向量库在线

python -m scripts.eval_retrieval --backend bm25 --sweep   # 关键词基线（离线、秒级）
.\scripts\dev.ps1 eval                                    # 容器内跑（需全栈）
```

口径：**文档级** `context recall@k = 命中 golden 文档数 / golden 文档总数`。
评价单位是"文档"而不是"块"，因为用户关心的是"该看的那篇有没有被找到"；
压成文档级还能让"一个文档的多块占满 top-k"这种退化现象无处藏身。

#### 已实测：关键词单路基线（2026-10-16，无需模型）

| k | recall@k（严格） | recall@k（图谱一跳扩展） | hit_rate@k |
|---|---|---|---|
| 1 | 0.5590 | 0.8542 | 0.7083 |
| 3 | 0.7639 | 0.9861 | 0.9375 |
| 5 | **0.8646** | 1.0000 | 0.9375 |
| 10 | 0.9236 | 1.0000 | 0.9792 |
| — | MRR **0.8194** | | |

> 测量条件：BM25（CJK 字符二元组分词，k1=1.5 / b=0.75）· 468 块 · 52 条题（48 条有 golden）·
> 候选池 20 · 单机单进程 · 2 ms/题 · 产物 `eval_output/retrieval_bm25_baseline-bm25-longform_*.json`

**这张表本身就是一个结论：提升空间在 @1，不在 @5。**

BM25 在 @5 已经有 0.86，靠堆 recall@5 讲不出故事；但 @1 只有 0.56 ——
也就是说"目标文档排在第一位的比例"刚过一半。**Rerank 的价值主要在 @1 与 MRR**，
如果只报 recall@5，重排的贡献会被严重低估。所以评估脚本一次给出 4 档 k + MRR，
而不是只报一个 @5。

##### 这个 0.8646 不能直接和旧基线 0.8519 比

同一批题、同一口径才有可比性。本轮同时改了两样东西（语料 +5 篇长文档、切块器修 3 个 bug），
所以必须把题目集拉齐再看：

| 口径 | recall@5 | MRR | 说明 |
|---|---|---|---|
| 旧基线（105 篇 / 402 点 / 36 题） | 0.8519 | 0.7936 | 上一轮记录 |
| **新语料（110 篇 / 468 点）· 同样这 36 题** | **0.8194** | **0.7593** | 苹果对苹果 → **退化 3.25pp** |
| 新增 12 道长文档题 | 1.0000 | 1.0000 | 12 题全部首命中位次 = 1 |
| 合起来的 52 题 | 0.8646 | 0.8194 | **不能读成"提升了"** |

合并后的 0.8646 高于 0.8519，纯粹是**题目集构成变了**：新加的 12 道长文档题
都是单 golden、专有词密度高，BM25 一击即中，把均值抬了上去；
而原有那 36 题在共享口径下其实**退化了 3.25pp**。只报合并数字就是自欺。

逐题 diff 定位到退化只有 3 题、且 0 题改善：

| 题号 | recall@5 | 归因 |
|---|---|---|
| q-4（熔断器参数） | 1.00 → 0.50 | 与长文档无关：top-5 里没有新长文档，是切块器修复改变了短文档块的上下文头文本 |
| q-14（慢 SQL → 连接池 → 雪崩） | 0.33 → 0.00 | `pm-payment-outage-2026q2`、`spec-tracing-standard-v2` 挤进 top-5 |
| q-27（风控 fail-open） | 0.67 → 0.33 | 同上两篇长文档挤进 top-5 |

**结论：2 题退化是长文档作为"真实干扰项"造成的，1 题是切块文本变化造成的。**
这正是加长文档想要的压力 —— 语料变得真实之后，BM25 的短板开始暴露，
向量召回 + 重排的收益才有可量化的对照空间（原来 @5=0.85 的"高分"里，
有相当一部分是语料太短太干净换来的）。

**另一个必须一起报的口径**：图谱一跳扩展。多模态题标的是"该看哪篇处置文档"，
而向量库里先被召回的是截图块；不允许扩展会把这类题算成失败，那不是检索错了。
两个数字都给才是诚实的。

> ⚠️ 已知局限：扩展口径在 k=1 就能到 0.85，是因为它把 top-1 文档的**所有邻居**都算进来，
> 于是"边多的文档"天然占便宜。这个指标可以用来证明图谱的价值，但不能单独作为
> 检索质量的主指标 —— 主指标必须是严格口径。

#### 已实测：向量召回 + 融合（2026-10-16，Qdrant 468 点，真实模型）

一条命令产出全部格子（模型只加载一次）：

```powershell
python -m scripts.eval_matrix --vector-source qdrant --parity
python -m scripts.eval_matrix --vector-source qdrant --rerank    # 追加「加重排」列
```

| 口径 | recall@5 | 扩展口径 | hit_rate@5 | **MRR** | 召回 P50 |
|---|---|---|---|---|---|
| BM25（基线） | 0.8646 | 1.0000 | 0.9375 | 0.8194 | **1.9 ms** |
| 向量（Qdrant） | 0.8750 | 0.9792 | 0.9583 | **0.8667** | 599 ms |
| 向量 + BM25 + RRF | **0.8958** | 0.9792 | 0.9583 | 0.8660 | 545 ms |
| 向量 + BM25 + RRF + **重排** | **0.9201** | **1.0000** | **0.9792** | **0.8873** | 64.7 s/题 |

> 最后一行是**全套 48 题**实测，但它的收益有 72% 会被"进上下文"的块数预算吃掉 ——
> 见下面「重排那一格」的**线上口径**，那一节才是结论。

增量（同一批 52 题、同一条链路，每次只多一个东西）：

| 步骤 | Δrecall@5 | ΔMRR | 怎么读 |
|---|---|---|---|
| BM25 → 向量 | **+1.04pp** | **+4.72pp** | 覆盖只涨一点，**排名涨得多** |
| 向量 → 向量+BM25+RRF | **+2.08pp** | −0.07pp | 覆盖继续涨，**排名不再涨** |

**三个反直觉的结论，外加一个"收益被吃掉"的发现，都值得在面试里讲。**

**① 向量的主要贡献是"排得准"，不是"找得到"。**
recall@5 只涨 1.04pp，但 MRR 涨 4.72pp（0.8194 → 0.8667）。
这正好印证了 §6.4 上一节从 BM25 基线推出的预测 —— *提升空间在 @1，不在 @5*。
如果只报 recall@5，向量这一路的价值会被低估 4 倍以上。

**② RRF 融合提升覆盖，但不提升排名。**
recall@5 涨 2.08pp（0.8750 → 0.8958），MRR 却几乎没动（−0.07pp）。
原因很具体：RRF 是**排名融合**，它擅长把"两路都命中"的文档顶上来，
但两路都命中的文档本来就在前列 —— 所以对 @5 有帮助（它能把某一路漏掉的
文档从另一路捞进池子），对"第一名"却几乎无影响。
**想让 MRR 再上一个台阶，只能靠重排，不能靠加召回通道。**
这句话可以直接回答"你为什么还要上 Reranker"。

**③ 图谱扩展口径反而降了（1.0000 → 0.9792），这是好事。**
BM25 的 1.0000 是"top-5 的邻居恰好覆盖了所有 golden"——典型的
**指标饱和**（恒为 1.0 的指标没有分辨力，见上一节的局限说明）。
向量召回换掉了部分 top-5 文档后它就掉到 0.9792，说明这个口径
确实对 top-5 的构成敏感，而不是一个恒真式。主指标仍然必须是严格口径。

**④ 重排的收益有一大半会被"进上下文"的块数预算吃掉。**
pool 口径下重排 +2.43pp，但换到**用户实际拿到的上下文口径**只剩 **+0.69pp**，
低于 48 题下 2.08pp 的分辨率 —— 也就是说，在当前预算下**证明不了它对用户有帮助**。
真正该先动的是上下文预算（`rerank_max_per_doc` / `top_k_rerank`，几乎免费），
而不是排序模型（64.7 s/题）。详见下一节「重排那一格」。

> 这一条比前三条更容易被追问，因为它推翻了一个很常见的直觉：
> "更贵的排序模型 = 更好的答案"。**预算不变时，更好的排序没有地方体现。**

> 测量条件：BGE-small-zh-v1.5（512 维）· Qdrant 单机（`ops_kb` 集合，468 点）·
> BM25 CJK 二元组 · 52 条题（48 条有 golden）· k=5 / 候选池 20 / 每路文档上限 3 ·
> 单机单进程 CPU · 产物 `eval_output/matrix_qdrant_*.json`。
> 召回率不受算力影响（同样的检索结果）；**延迟绝对值在这台机上不可复现，
> 只可看同轮内的相对差异** —— 原因见「冷启动与延迟分解 · 先说清楚这台机器的算力」。

#### 「一致性校验」：两条向量路径必须给出同一个答案

评估脚本可以选择走**本地暴力扫描**（读 build 落盘的 `vectors.bin`）还是走**向量库**。
这两条路径是天然的控制组 —— 同一个模型、同一批向量、同一种余弦相似度，
差异只应来自近似索引（HNSW）的取舍。

```powershell
python -m scripts.eval_matrix --vector-source qdrant --parity
```

实测结果：

| 项 | 值 |
|---|---|
| top-5 文档平均重合率（Jaccard） | **1.0000** |
| 本地暴力扫描 recall@5 | 0.8750 |
| 向量库 recall@5 | 0.8750 |
| 向量库降级题数 | 0 |

完全一致。这条校验通过，上面那张矩阵表里的向量格才**有资格**被引用 ——
如果不一致，问题在向量库侧的距离度量/归一化/过滤配置，而不在模型。
这也是 `brute_force_search` 这个"看起来多余"的函数存在的理由。

#### 为什么评估脚本不再自己实现检索（本轮最重要的重构）

之前 `eval_retrieval.py` 自己写了 `rrf_fuse`、自己拼 BM25 排名、自己扩池重排。
结果是"评估"与"线上"成了两条代码路径，差异**至少**有四处：

| 环节 | 旧评估脚本 | 线上（`app/pipeline/rag.py`） |
|---|---|---|
| `kb_version` 过滤 | ❌ 无 | ✅ 有（store 侧 + Chunk 侧双保险） |
| 每路文档占比上限 | ❌ 无 | ✅ 有（**融合之前**施加） |
| 融合粒度 | 文档级 | 块级 |
| 重排候选池 | 命中文档的**全部**块 | 只重排已召回的块 |

四处偏差方向不一致：没有版本过滤会**虚高**（旧版本点也算命中），
扩池重排也会**虚高**（候选更多），而没有占比上限会**压低**。
叠在一起得到的数字既不是线上的、也不是干净的消融 ——
它是**某个别的系统**的分数。而这类偏差**不报错、不告警、不使任何测试变红**。

现在：检索**全部**走服务端的 `recall_detailed`，消融变成 `RecallConfig` 的
显式字段（哪些通道 / 是否融合 / 池子多大 / 向量索引来自哪里）。
差异是**参数**，不是代码分叉。`tests/test_recall_convergence.py` 里的
`test_eval_calls_serving_recall` 会盯着这件事 —— 有人重新在 eval 里写一份检索，它会红。

**重排要报两个口径，因为它们是两个不同的东西：**

| 口径 | 定义 | 回答的问题 |
|---|---|---|
| `pool` | 重排分当排序信号，池内全部候选重排后取 top-5 文档 | 重排的**排序**有没有变好（与未重排基线唯一变量可比） |
| `served` | 线上真实截断（每篇最多 2 块、只带 5 块进上下文）后投影到文档级 | 用户**实际拿到**的上下文里有没有目标文档 |

只报 `pool` 会让重排显得比实际更好；只报 `served` 又无法与基线对比。
两个都打出来，并注明块数上限这个变量。

#### 重排那一格：收益、代价，以及一个反直觉的结论

全套 48 题（`--only RRF`，± `--rerank`；同批题、同一条链路，只改"是否重排"这一个变量）：

| 口径 | pool recall@5 | 线上口径 recall@5 | MRR(pool) | MRR(线上) |
|---|---|---|---|---|
| 向量+BM25+RRF | 0.8958 | **0.8785** | 0.8660 | 0.8785 |
| 向量+BM25+RRF **+ 重排** | **0.9201** | **0.8854** | **0.8873** | **0.8854** |
| **增量** | **+2.43 pp** | **+0.69 pp** | +2.13 pp | +0.69 pp |

**两个口径给出的答案完全不同，而这正是重点。**

- **pool 口径**（重排分当排序信号，池内全部候选重排后取 top-5 文档）：**+2.43 pp**
- **线上口径**（每篇最多 2 块、只带 5 块进上下文）：**+0.69 pp**

**重排约 72% 的收益在"进上下文"这一步被丢掉了**（留存率 0.0069 / 0.0243 ≈ 28%）。

再看截断本身的代价 —— 它在加了重排之后**翻了一倍**：

| 配置 | pool | 线上 | 截断吃掉 |
|---|---|---|---|
| 不重排 | 0.8958 | 0.8785 | −1.73 pp |
| 加重排 | 0.9201 | 0.8854 | **−3.47 pp** |

原因不神秘：重排把一批**内容更相关、但来自同一篇文档**的块提到前面，
而"每篇最多 2 块"正好把它们砍掉。
**排序变好了，但预算没变，好排序就没地方体现。**

> 📌 **结论（这一节最值得讲的一句）**
>
> 48 题下 1 题 = **2.08 pp**，而线上口径的增量只有 **0.69 pp** ——
> **低于测量分辨率**。也就是说：在当前上下文预算下，
> **无法证明重排对"用户实际拿到的上下文"有可测量的帮助**。
>
> 瓶颈是**上下文预算**（`rerank_max_per_doc=2` / `top_k_rerank=5`），
> **不是排序模型**。正确顺序是先放开预算（几乎免费），再考虑为排序付费。
> 而重排的代价是实测 **64.7 s/题**（`rerank_ms_avg`）。

**为什么必须两个口径都打出来：** 只报 pool，你会得出"重排 +2.43 pp、值得"；
只报线上，你没法与基线比。**两个一起看，才知道收益是被谁吃掉的。**

> ⚠️ **一个已修正的口径漏洞**：早期实现只对**重排格**算线上口径，
> 非重排格直接沿用 pool 值 —— 那等于拿「重排 + 真实截断」
> 比「不重排 + 不截断」。后者在线上根本不存在（`RERANK_ENABLED=false` 时
> 走 `_no_rerank_order()`，照样先限每篇块数再截断），
> 所以那个对比会**系统性偏袒重排**。现已修正为**每一格都算线上口径**
> （`scripts/eval_retrieval.py::run_cell`）。

##### 小样本是怎么骗人的（一个真实例子，留着当反面教材）

先前只跑了前 15 题，得到 pool 0.9333 / 线上 0.9333 —— **两个口径完全相等**，
看起来"线上截断没有任何损失"。扩到全套 48 题后，截断的代价是 **−3.47 pp**。

15 题时 1 题 = 6.7 pp，−3.47 pp 这种量级的差异**根本不可能被分辨出来**。
所以：**小样本不是"结论弱一点"，是会给出方向性错误的结论。**

**为什么当初是 15 题而不是 52 题：重排一格要跑约 50 分钟。**
52 题 × 56 s/题（机器空闲时实测前 4 题为 223 s，线性外推；
有其它进程争用时实测约 66 s/题 → 临近 1 小时）—— 而"没人愿意跑的实验
等于不存在"。所以脚本提供 `--only` 与 `--limit` 两个筛选开关：

```powershell
# 只跑 RRF 这一行的 ±重排（其余格子跳过）
python -m scripts.eval_matrix --vector-source qdrant --rerank --only RRF

# 想先看方向再决定要不要跑全套（⚠ 小样本会给出方向性错误的结论，仅用于估时）
python -m scripts.eval_matrix --vector-source qdrant --rerank --only RRF --limit 15
```

`--limit` 会打 `WARNING` 并在产物 JSON 里写 `"limited_sample": true` ——
**小样本不会被误当成全套结论**，因为它自己就声明了。

> 全套产物：`eval_output/matrix_qdrant_rerank_20261016-210136.json`（含重排）
> 与 `eval_output/matrix_qdrant_20261016-210443.json`（不含重排，用于同口径对比）。

---

### 意图路由（规则优先，可评估）

```powershell
python -m scripts.check_intent --min-accuracy 0.94 --min-nonrag-recall 0.85
```

四类意图，决定链路**怎么走**：

| 意图 | 链路 | 例子 |
|---|---|---|
| `rag` | 向量 + BM25 + 融合 + 重排 → 生成 | "连接池耗尽了怎么处理" |
| `tool` | MCP 工具调用 → 生成（不检索） | "INC-2026-0412 处理到哪了" |
| `hybrid` | 召回与工具**并发** → 合并两段上下文 | "SRV-0088 现在什么状态，处置文档怎么说" |
| `chitchat` | 固定话术，**不走模型、不落缓存** | "你好，你是谁？" |

**为什么规则优先而不是上 LLM 分类。** 三个理由，按重要性排序：

1. **可评估。** 规则可以对着 52 条标注集算准确率、算混淆矩阵、
   进 CI 卡阈值。LLM 分类要先花一次调用、再有随机性，
   评估成本高一个量级，而且"这次的 0.92 和上次的 0.94 是不是同一个模型"说不清。
2. **可解释。** 决策带 `reason` 与 `signals`，会随 `route` 事件推给前端
   （演示时可以直接指着屏幕说"它为什么判定这是实时类问题"）。
3. **可预测。** 路由错了会直接改变答案的**性质**，
   把它放进一个带随机性的组件里，等于让最不该抖的一环去抖。

LLM 兜底留了开关（`INTENT_LLM_FALLBACK=false`），**默认关闭** ——
没有评估支撑的兜底只是"多一次调用"，不是"更准"。

#### 误判代价是不对称的

| 误判方向 | 后果 | 严重性 |
|---|---|---|
| `rag` → `tool` | 拿实时数据回答知识问题，**答案凭空错** | 🔴 不可察觉 |
| `tool` → `rag` | 检索不到工单，模型如实说"资料未涉及" | 🟡 可恢复 |

所以默认偏向 `rag`，只有在**明确出现实时信号**时才判为 live。
这个不对称是整个规则集的设计约束，不是微调出来的。

#### 实测准确率

| 口径 | 结果 |
|---|---|
| 标注集全量 | **52 / 52 = 1.0000** |
| 其中非 rag 类（tool + hybrid + chitchat） | **7 / 7 = 1.0000** |
| 手写对抗集（19 句，**从未用于调参**） | **19 / 19 = 1.0000** |

> `python -m scripts.check_intent` 会同时打印"全部判为 rag 能得多少分"= **0.8654**。
> 这个提示是刻意加的：52 题里 45 题是 rag，一个**只会输出 rag 的函数**就有 86.54% 准确率。
> 所以**准确率单独报没有意义**，必须同时报非 rag 类的召回率 ——
> 后者才是路由真正在做的事。

对抗集独立存在的理由：规则是在那 52 条标注上调出来的，
拿同一批数据报准确率有**乐观偏差**。对抗集是手写的、故意不像标注措辞的句子，
它通过才说明规则**泛化**了而不是**记住**了。
`tests/test_intent.py` 的回归断言用的是**阈值**（0.94 / 0.85）而不是 1.0 ——
对着标注集调参到 1.0 是过拟合，不是成就。

#### 调参过程中真正踩到的三个坑

**① 正则的空分支会匹配一切。**
`LIVE_VERBS` 里一个多余的 `|` 让分支变成 `(?:a|b|)`，而空串永远匹配 ——
于是**每一句话都被判为实时类**，准确率从 1.0 崩到 **0.1346**。
这个 bug 唯一被发现的理由是"第一次跑出来的数字荒唐得不像是真的"。
教训：正则写完后必须显式验证它**不匹配**什么。

**② 裸动词会误伤专业术语。**
`查询` 无条件命中，于是"**慢查询**怎么优化"被判成实时类。
修法是让动词**必须带宾语**（`查询(工单|资产|告警|日志|...)`）。
同一类问题还有 `此刻` + `告警`：一个讲"监控大盘此刻同时告警说明什么"的
**知识问题**被误判成实时告警查询。修法是把 工单/告警 从实时宾语集合里**移出去** ——
它们既可能是"查一下某张工单"，也可能是"工单流转规则是什么"。

**③ 闲聊要按小句匹配，不能整串匹配。**
`你好，你是谁？你能帮我做什么？` 是三个小句，
整串正则匹配不上，于是被判成 `rag` 去检索了一轮。
修法是按标点切小句，**要求每个小句都是闲聊**才算闲聊
（出现任意一个实质小句就不是）。

---

### 工具层：MCP 优先 + mock 降级，且 `call()` 永不抛异常

5 个工具，签名集中定义在 `app/tools/providers.py` 的 `TOOL_SPECS` ——
`MockProvider` 与 `McpProvider` 共用同一份定义，所以"mock 能用、MCP 对不上"
这种错配在**启动时**就会暴露（`install()` 走 `getattr`，缺实现直接炸）。

| 工具 | 用途 |
|---|---|
| `query_ticket` | 按工单号查详情 |
| `search_tickets` | 按关键词搜工单 |
| `query_asset` | 按资产编号查状态 |
| `query_alerts_by_service` | 查某服务**未关闭**告警 |
| `list_open_tickets` | 列未结单工单 |

**`call()` 永不抛异常** —— 未知工具 / 超时（默认 3 s）/ 参数错误 / 下游异常，
全部收敛成 `ok=False` 的 `ToolResult`。

这不是"吞异常"。关键区别在于：**失败的调用会被渲染进 LLM 上下文**，
所以模型会说"未查询到"而不是编造 —— 一个真实的 0 条和一个查询失败，
对用户是两件必须区分的事：

```
query_alerts_by_service(service=inventory-service)
  → 服务 inventory-service 的未关闭告警：0 条（该服务当前没有未关闭的告警）
```

**`source` 字段是诚实性要求。** 每个结果带 `mock | mcp | cache | none`，
并随 SSE 推给前端 —— 演示时用的是 mock 数据，界面上会如实标注是 mock，
不会让观众误以为是实时数据。

**MCP 优先 → mock 降级放在调用点，不在 MCP 客户端内部重试。**
D1 阶段 Java MCP Server 还不存在，所以"探测失败就用 mock"；
探测结果有 30 s 的 TTL 缓存，避免每次调用都去 ping 一个已知不存在的服务。

**缓存只缓存成功。** 30 s TTL 的结果缓存按**排序后的参数**拼 key
（否则 `(a,b)` 与 `(b,a)` 会算成两个 key）；失败**不缓存**，
否则一次网络抖动会被放大成 30 秒的持续失败。

**只返回 `firing` 的告警。** 把 `resolved` 也返回会让模型把已经恢复的
故障汇报成"正在发生"—— 这类错误看起来非常真实，因而非常危险。
标注集 q-36 专门测这一点，语料里的告警数据也是**按这个断言构造**的
（`inventory-service` 只有 resolved、没有 firing），
所以"没有未关闭告警"这个答案与数据自洽，而不是靠模型猜。

---

### SSE 事件协议（前端与压测脚本的契约）

| 事件 | 时机 | 关键字段 |
|---|---|---|
| `meta` | 最先 | `cache`（exact/semantic）、`similarity`、`kb_version`、`trace_id` |
| `route` | 紧随其后 | `intent`、`reason`、`signals`、`entities` |
| `tool_call` | 调用前 | `tool`、`args` |
| `tool_result` | 每次返回 | `ok`、`source`、`elapsed_ms`、`summary` |
| `tool_summary` | 全部结束 | `ok_count` / `total` |
| `docs` | 检索完成 | `sources`、`pages`、`recall_sources`、`channels`、`fused`、`scores`、`order_by`、`rank_scores` |
| `token` | 流式正文 | `content`（逐片，前端直接 append） |
| `degraded` | 任一环节降级 | `stage`、`reason` |
| `error` | 致命 | `message` |
| `done` | 收尾 | `cache`、`route`、`stage_ms`、`total_ms` |

`docs.scores` 与 `docs.rank_scores` **不是一回事**，这是刻意分开的：

- `scores` = 每块在**自己那一路**的原始分。向量是 cosine（0~1），BM25 无上界
  （实测 `5.9818` 很常见）——量纲不同，放在一个数组里**本来就不单调**；
- `order_by` + `rank_scores` = **真正决定顺序的那个分**：`rerank`（重排分）/
  `rrf`（融合分，只看排名）/ `score`（单路退化）。要验证"排序生效"必须用它。

用 `scores` 判断排序会得到一个非常像 bug 的假信号：实测
`[0.6466, 0.7045, 0.6619, 5.9818, 4.833]` —— 顺序完全正确（RRF 融合），
但前三个数确实不递减。

三处非显然的设计：

**① 心跳用 `asyncio.wait` 轮询，不是 `wait_for`。**
`wait_for` 只能表达"最多等 N 秒然后超时"，表达不了"还没好就继续等"。
nginx 默认 60 s 空闲就断连接，长回答（尤其带工具调用）会超过这个时间。
所以每 15 s 推一个 `: keepalive` 注释帧。压测时这条直接决定"连接是不是被中间层杀掉的"。

**② `kb_version` 从缓存取，不从 settings 取。**
`settings.kb_version` 是**进程启动时的快照**。`/admin/kb/burn` 把版本号 +1 之后，
settings 里还是旧值 —— 于是 SSE 事件里的版本号会**撒谎**。
它必须从缓存读运行时值。

同一条道理也适用于探针，所以 `/health` 把字段改名为 `config_kb_version`（它真的只是
快照），运行时值由 `/ready` 顶层 `kb_version` + `checks.cache` 报。改名直接来自一次
自测日志：`/health` 说 1、SSE `meta` 说 2，同一个字段名指向两个值，看的人会以为 bump 没生效。

`/ready` 的 `checks.cache` 还会在**索引版本 ≠ 缓存版本**时明说：`/admin/kb/bump`
只动 Redis 不动 `data/index/kb_version.json`，bump 完没重建索引就等于全量冷启动 ——
正确性没事，但"缓存为什么全 miss"必须能一眼看出来，而不是去翻 Redis key。

**③ 闲聊短路：不走模型、不写缓存。**
问"你好"也走一遍检索 + LLM 是纯浪费，而且回答不稳定。
闲聊用固定话术直接返回，且**不落缓存** —— 因为它本来就没有成本，
落缓存只会污染命中率统计（让命中率看起来虚高）。

`tests/test_chat_sse.py` 断言的是事件的**相对顺序**，不是集合。
理由：事件顺序就是前端的渲染逻辑，顺序错了前端不报错、只是显示得不对 ——
这类问题人工点几下基本看不出来。

---

### LangGraph 编排与三层记忆（D1 下午）

旧的实现是 `chat.py` 里一个手写 async generator：路由 → 缓存 → 分支 → 生成，
状态靠函数参数在调用栈里传。链路短时这样最简单，但要叠三层记忆、
断点续跑、工具人工确认时，它会膨胀成难以维护的隐式状态机。
编排已重构为 **LangGraph 状态机**（`app/pipeline/orchestrator.py`）：

```
START → route ──chitchat──▶ chitchat → END
          │ main
          ▼
     memory_load → cache_exact ──exact 命中──▶ END
          │             │ miss（exact 命中不付编码费）
          │             ▼
          │           embed → cache_semantic ──semantic 命中──▶ END
          │             │ miss（编码一次，语义缓存与向量召回共用）
          │             ▼
          │        singleflight ──跟随者命中──▶ END
          │             │ 领跑者
          │             ▼
          │          dispatch ──┬──▶ tools ──┐
          │                     └──▶ recall ─┴─▶ tools_done ──┬──▶ generate → END
          │                                                   └──(有挂起)──▶ await_confirm → END
```

**重构的验收标准是"SSE 事件协议一字不改"**：`tests/test_chat_sse.py` 的
端点级用例不改断言、只改两个 patch 目标（`embed_one` / `plan` 随编排器搬家），
全部原样通过 —— 事件顺序错了前端不会报错，只会显示得不对，
所以"协议不变"必须是测试能抓住的性质，而不是口头承诺。

三个值得讲的实现决策：

**① 节点内的流式输出走 `stream_mode="custom"`。**
token 是逐段产生的，节点不能等整段生成完再返回状态。
每个节点经 `get_stream_writer()` 把帧推给外层 SSE 生成器，端点只透传 ——
协议归编排器管，端点只做鉴权 + 并发闸门。

**② 扇出并行由图结构保证，不靠手工起任务。**
hybrid 的 tools 与 recall 两路无依赖，`dispatch` 节点扇出、LangGraph
在 `generate` 前自动汇合。旧的"先启动召回任务再跑工具"手工并行被
`test_orchestrator_graph.py::test_fanout_is_parallel` 钉住 ——
如果有人把扇出改回串行，事件顺序不变、只有延迟悄悄变成两路相加，
端点用例抓不住它，拓扑测试能。

**③ 记忆降级事件必须"补发"而不是"抢发"。**
记忆是唯一可能**在 meta 之前**就失败的环节（`memory_load` 排在缓存查询前），
而 meta 必须是第一帧。所以 load 失败先记进 state 的 `degraded`，
由携带 meta 的节点在 meta 之后补发 `stage=memory` 事件。

三层记忆的分界（代码对应 `app/memory/`）：

| 层 | 承载 | 生命周期 | 位置 |
|---|---|---|---|
| 工作记忆 | `AgentState`（路由决策/向量/召回块/降级清单） | 单次请求 | 进程内（LangGraph state） |
| 会话记忆 | 最近 N 轮问答（默认 10，滑窗截断） | 24 h | Redis list `ops:sess:{sid}:hist` |
| 长期事实 | 规则抽取的稳定属性（角色/技术栈） | 7 天 | Redis hash `ops:sess:{sid}:facts` |

- **进程内零会话状态**：多副本横向扩展时任意副本可接任意请求 ——
  这是"3 副本吞吐提升"那条指标的前提，没有它压测数字不成立。
- **事实抽取是规则版 v1**：LLM 抽错一条事实，它会在后续每轮都被注入
  prompt，错误半衰期是整个会话且用户看不到；规则版覆盖率低但每条
  可解释、可单测，且带**否定护栏**（"我们不用 MySQL"不进事实库）。
  LLM 抽取（带置信度+确认）在 backlog。
- **记忆故障的三重防御**：store 层吞异常（load 返空/save 返错误串）→
  编排层兜 store 违约 → SSE 补发 `stage=memory` 降级。
  记忆静默失效是最危险的失败模式 —— 请求照常返回，只是不带历史，
  用户根本不知道有这回事。
- **总开关 `SESSION_MEMORY_ENABLED=false`**：压测用它把记忆层排除在测量外。

> ⚠️ **尚未接 checkpointer**：LangGraph 的断点续跑/时间旅行依赖
> checkpointer，官方 Redis 实现需额外安装包（离线环境暂未引入），
> 暂以自管 Redis 会话层替代。接入时只需安装依赖并把
> `thread_id=<session_id>` 传入 `astream` 的 config，图结构无需改动。
> 列入 §11 backlog。

---

### 冷启动与延迟分解

`app/main.py` 的 lifespan 预热顺序是有讲究的：

```
cache → tools → 索引产物预读 → **向量库预热** → 模型预热
```

**向量库预热是必须的，不是优化。** 实测（本机 Windows，468 点）：
Qdrant 的**首次**查询要 **8.9 秒** —— 服务端要把 HNSW 索引加载进内存，
之后稳定在数百毫秒。不预热的话：

- 第一个真实用户要白等这 9 秒；
- 更糟的是**压测场景**：阶梯加压的第一档就是第一个请求，
  这 9 秒会被当成"这一档并发撑不住"的证据 —— 一个纯粹的误判，
  而且它看起来很像真的。所以预热是**让后续测量有效**的前提。

**模型加载是大头，且它是 import 不是推理。** 本机实测：

| 模型 | 首次可用耗时 | 其中权重读取 |
|---|---|---|
| `bge-small-zh-v1.5`（向量） | 约 110 s | 几秒 |
| `bge-reranker-base`（重排） | **87.1 s** | 几秒 |

绝大部分时间是 `sentence-transformers` / `torch` 的**导入与初始化**
（权重实际读取只要几秒）。所以：

- 容器启动时间会很长，`docker-compose` 健康检查的 `start_period` 必须给够，
  否则容器会被反复判定为不健康并重启；
- 这个代价只付一次，**不能**混进 TTFT —— 那正是"TTFT 基线是假的"的经典来源。

#### 一个实测踩到的坑：模型已缓存，却仍在联网

即使权重已经在本机缓存里，`sentence_transformers` 初始化时**仍会向 Hub 发
HEAD 请求**去核对文件元数据。在 `HF_ENDPOINT=hf-mirror.com` 下实测会遇到
`HTTP Error 429` + 指数退避重试，把模型加载时间从几十秒拖到几分钟，
而且**看起来像卡死**。

本项目的处理是分层的，这里必须区分清楚，否则会踩到反效果：

| 运行位置 | 配置 | 说明 |
|---|---|---|
| 容器内 `api` | `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` | 镜像/卷里已有模型，**必须**离线，避免每次启动都去联网（私有化部署断网也能起） |
| 容器内 `build-kb` | 只给 `HF_ENDPOINT`，**不给** offline | 一次性任务要能在**首次**把模型下下来，强制离线会让冷启动直接失败 |
| **宿主机脚本** | 当前**没有**设置 | 所以 `eval_matrix` / `bench_rerank` 在宿主机跑会吃到 429 |

宿主机跑评估时建议显式离线（模型已缓存的情况下）：

```powershell
$env:HF_HUB_OFFLINE = "1"
python -m scripts.eval_matrix --vector-source qdrant --parity
```

> ⚠️ **首次部署的顺序是有依赖的**：`api` 容器带 `HF_HUB_OFFLINE=1`，
> 所以它**要求模型已经在 `huggingface_cache` 卷里**。空机器上必须先跑一次
> `.\scripts\dev.ps1 kb`（`build-kb`，能联网下载），再起 `api`；
> 否则 `api` 会以一个"模型加载失败"的姿态启动 —— 而这时容器本身是
> healthy 的（健康检查只探 HTTP），要等第一个请求才会暴露。
>
> **判断与修复都有一条命令**：
> `curl.exe -s http://localhost:8080/ready` 会直接报 `embedding_model.ok=false`
> （这就是"假健康"的探针，`/health` 只代表进程活着）；
> 本机已缓存模型的场景不必联网，`.\scripts\dev.ps1 models` 把宿主
> `~/.cache/huggingface/hub` 灌进命名卷，再 `docker compose restart api` 即可。

#### 单题召回延迟（同一轮、同一机器、52 题，P50）

| 口径 | 召回 P50 | 相对 BM25 |
|---|---|---|
| BM25 | **1.9 ms** | — |
| 向量（Qdrant） | 599 ms | +597 ms |
| 向量 + BM25 + RRF | 545 ms | +543 ms |

向量的开销主要在 embedding（CPU 上单条 query 约 0.1~1 s）而不在 Qdrant 本身。
**RRF 几乎不增加延迟**（545 ms 甚至略低于向量单路，因为两路是并发 `gather` 的）——
这是一个值得主动说的点：融合的代价是"多跑一路"，不是"串行等待"。

#### ⚠️ 先说清楚这台机器的算力，否则上面的数字会被误读

**这一节是一次自我纠错，写下来比藏起来有用。**

最初的版本里我写的是"本机 matmul 标尺 257 ms，比正常桌面慢约 6.4×"。
后来把同一条命令在**不同时间**重复了三次，得到的是：

| 轮次 | matmul 标尺 | 20 对重排 |
|---|---|---|
| 19:03 | **257 ms** | 29.6 s |
| 19:54 | **65.5 ms** | 44.5 s |
| 20:01 | **34.4 ms** | 42.4 s |

标尺在**一小时内漂了 7.5 倍**，而 20:01 那轮里 **29 对比 20 对还快**
（36.1 s vs 42.4 s）—— 这物理上不可能，只能说明测量本身不可复现。

结论有两条，第二条比第一条重要：

1. **"慢 6.4×" 是错的，撤回。** 那一次标定本身就是在 CPU 争用下测的
   （257 ms 里有我自己的并发任务）。本机真实算力大致在"正常桌面水平
   ~1.6×"这一档，而不是一个数量级。
2. **真正的结论：这台机器（共享 vCPU）的绝对延迟不可复现。**
   所以本节表格里的毫秒值**不应作为可引用数字**：
   - ✅ **可信**：同一轮里各口径的**相对差异**（BM25 ↔ 向量 ↔ RRF）
   - ✅ **可信**：召回率 / MRR 等**质量指标**（不经过计时器，与算力无关）
   - ❌ **不可引用**：任何绝对毫秒值 —— 换台机器或换个时间点都对不上
   - ❌ **不要**把 599 ms 当成"向量检索很慢"：Qdrant 本身在百毫秒内，
     这 599 ms 里绝大部分是 CPU 上的模型推理

`scripts/bench_rerank.py` 仍然会把这条标尺连同结果一起写进产物，
但它的用途变了：**不是"给你一个折算倍率"，而是"告诉你这次的数字有多大不确定性"**。
延迟数字不标注机器状态就等于没有数字，而"16 核"这种参数并不能说明算力 ——
共享 vCPU 上，同一台机的实际算力可以一天之内差好几倍。

> 📌 **这本身就是一个该讲给面试官的方法论**：先用一条与业务无关的
> 基准（matmul）标定机器，再决定"哪些数字可以引用、哪些只能看趋势"。
> 我一开始漏了这一步，把争用下的数字当成了机器属性。

#### 重排的代价：本轮最值得讲的一个取舍

```powershell
python -m scripts.bench_rerank --from-recall 20
```

**先纠正我自己犯的一个错：候选池不该写死。** `--pairs 20` 里的 20 是人为指定的，
而线上真正送进重排的块数由 `top_k_retrieve`（每路召回数）、两路重叠率、
`recall_max_per_doc`（每篇上限）共同决定 —— 不是常数。
实测（走**真实** `recall_detailed`，即线上那条路，只数块数不打分）：

| 统计量 | 候选池块数 |
|---|---|
| min | 21 |
| **中位** | **29** |
| max | 36 |
| 均值 | 28.7 |

即默认配置（`top_k_retrieve=20` + RRF）下，重排一次要打 **约 29 对**，
不是原先假设的 20 对 —— 原来的成本估算**低估了约 1.45 倍**。
（这也是为什么 `--from-recall` 会被加进脚本，而不是留一个手填的常数。）

修正后的代价 —— **三次独立测量的区间**，不是单值：

| 候选池 | 单次耗时 |
|---|---|
| 1 | 2.6 ~ 5.4 s |
| 5 | 8.6 ~ 19.0 s |
| 20 | 29.6 ~ 44.5 s |
| **29（线上实测中位）** | **36 ~ 45 s** |

**一个重要的交叉验证**：上面这张表是 *bench* 里用合成 (query, doc) 对测的，
而全套评测在**真实 52 题、真实链路**上实测 `rerank_ms_avg = 64.7 s/题`
（`eval_output/matrix_qdrant_rerank_20261016-210136.json`）。
两个数字同量级但差了约 1.5 倍，原因有两个，都值得知道：

1. 真实候选池分布有长尾（max 36 块），而 bench 用的是固定档位；
2. **bench 里一遍遍喂同一段文本，tokenizer 与注意力的 cache 行为更友好**，
   而真实题目长短不一。

→ 结论：**bench 给的是"代价的量级与形状"，报线上代价要用评测里的 `rerank_ms_avg`。**
（这也是为什么两个数字都留在 README 里，而不是只留更好看的那个。）

> 测量条件：16 vCPU / torch 12 线程 / torch 2.14.0+cpu · `bge-reranker-base` ·
> 文档 460 字（对齐 `chunker.MAX_CHARS`）· 每档重复 3 次取中位数 ·
> 产物 `eval_output/bench_rerank_*.json`。
>
> **报区间而不是单值，是刻意的**：同一台机器的算力标尺在一小时内漂了 7.5 倍
> （见上一节），给一个 3 位有效数字的单值等于给出虚假精度。
> 这台机器上**能报的是量级（几十秒/题），不是 29.6 这个数**。
>
> 早期在有 CPU 争用时还测到过 **53 s / 105 s / 166 s**，
> 以及一整轮被污染到 `rerank_ms_avg = 103 s` 的评测 —— 那些数字同理不可引用。
> 判据是同一批数据里的 `recall_ms_p50`：空闲时 545 ms，争用时 1649 ms，
> **质量指标不受影响，延迟指标直接作废**。

**结论：单次问答几十秒量级的重排，不能放在在线首答路径上。**
这不是"优化一下就好"，而是设计约束，必须给出处置方案：

| 处置 | 代价 | 本项目状态 |
|---|---|---|
| **一键关掉重排**（`RERANK_ENABLED=false`） | 丢失重排带来的排序收益 | ✅ 已实现（见下） |
| **用缓存摊薄**（`CACHE_RERANK_TTL`） | 同一 query 复用；首次仍慢 | ✅ 已实现 |
| 换更小的重排模型 / 量化 / ONNX | 精度下降，需重新评估 | 🔜 |
| 上 GPU | 成本 | 🔜 |
| 缩小重排候选池 | 召回率下降（候选少了，重排救不回来） | ⚙️ 可调 `--pool` |

**关掉重排这件事本身有个容易犯的错：静默跳过。** 所以实现上做了两件事：

1. 关闭后走的是**与"重排失败降级"完全相同的那一条路径**
   （`_no_rerank_order()`：先限每篇块数、再截断 —— 顺序不能反），
   不是另写一份"简化版"。两条路只可能有一种行为。
2. 关闭会在 SSE 里**如实标注** `degraded: rerank:disabled`。
   否则演示的人和看指标的人都不知道自己在看一条更弱的链路 ——
   那样得到的 TTFT 数字就不是"这套架构的 TTFT"，而是"少了重排的 TTFT"。

```dotenv
# .env
RERANK_ENABLED=false   # 压测 / 3 分钟演示时建议关掉，并在报告里注明
```

> ⚠️ **收益和代价必须一起报，而且代价要放在前面。**
> 只报"重排把 MRR 抬高了 2.1 个点"而不报"它要 64.7 秒一题"，
> 是最容易被一句话问住的表述：*"那你怎么不用最贵的方案？"*
> —— 因为贵到不能用。
>
> 完整的对照口径应该是这样一句话：
> **"重排让 recall@5 从 0.8958 到 0.9201、MRR 从 0.8660 到 0.8873
> —— 但那是在扩池口径下；换到用户实际拿到的上下文口径，增益只剩
> 0.69 pp，低于一题的分辨率（2.08 pp）。而代价是每道题多花 64.7 秒。
> 所以我默认把它关掉了。"**
>
> 注意这句里有两处主动交底，都不是被抓到才说的：
> ① 代价 64.7 s/题；② **收益在线上口径下几乎归零**。
> **先讲代价、再讲收益、主动说出收益的边界**，比反过来可信得多。


### MTTR

```powershell
python -m scripts.fault_inject --fault stop-qdrant --rounds 5
```
四个时间戳：T1 注入 → T2 告警 firing → T3 服务恢复。
- MTTR = **T3 - T2**（从"系统知道自己病了"到"系统能干活了"）
- 检测延迟 = T2 - T1

很多简历上的 MTTR 其实测的是 T4-T1，口径完全不同。把口径写清楚本身就是加分项。

### 加分指标（FDE 面试必问）

- 单次问答 token 成本 / 千次问答成本 → `cache_tokens_saved_total` 直接给出节省量
- rerank 引入的延迟代价 vs 召回率收益 → 主动讲取舍

---

## 6.x 全链路可观测：Langfuse + 本地 trace

一条请求的完整链路，现在能被还原成下面这棵树（`--only trace` 实测输出）：

```
turn                        total 3435ms · TTFT 1784ms · route=rag · cache=miss
├── route           1ms     intent=rag, conf=0.85, signals=[...]
├── memory_load   194ms     history_turns=2, facts=3
├── embed        1250ms     model=bge-small-zh-v1.5, dim=512
├── cache_lookup    1ms     hit=miss
├── singleflight    0ms     leader=true
├── recall       1160ms     channels={vector:20,bm25:20}, n_chunks=5, order_by=rrf
│   └── rerank              pool=20, kept=5          ← 最贵的一环，单独计量
├── generate        981ms   model=deepseek-chat, ttft_ms=1784, first_token_offset=981
├── cache_store   282ms     tokens_saved=1032
└── memory_save    21ms     facts=2
```

### 三个设计决策（都是被实测逼出来的）

**① 本地 JSONL 是一等公民，Langfuse 是可选上报。**

没有 key 也必须能追踪，否则"埋点到底埋上没埋上"这件事就依赖云端才能验证。所以：
本地 `data/traces/YYYY-MM-DD.jsonl` **全量**落盘（一个 span 一行），
Langfuse 按 `OBSERVABILITY_SAMPLE_RATE` **采样上报**。方向不能反过来 ——
本地也采样的话，"回放某次失败请求"就变成碰运气。

**② 子 span 不依赖 OTEL 的 contextvar。**

LangGraph 每个节点跑在独立 asyncio task 里，`with span:` 设的 current span
**不会**传播到下一个节点。实测（langgraph 1.2.12 + langfuse 5.0.2）：

```
node turn     -> get_current_trace_id() = 47cd2454...
node dispatch -> get_current_trace_id() = None      ← 丢了
```

所以子 span 一律用 `root.start_observation(...)` **显式挂 root**。
同理，root 不用 `start_as_current_observation`（它的 `__exit__` 会在另一个
Context 里执行，实测报 `Failed to detach context / Token was created in a
different Context`），改用非 current 的 `start_observation` + 手动 `end()`。

> **代价**：后端里 span 树是平的（root + N 个并列阶段）。逻辑父子关系
> （`parent=dispatch`）写在 `metadata.parent` 与 JSONL 里，回放脚本按它还原层级。
> 时间线（Gantt）是准确的，树深是 1 —— 这是取舍，不是疏漏。

**③ 上报不许进关键路径。**

实测：Langfuse 客户端**构造**在容器内 0.6s、Windows 宿主（SDK 5.0.1）**10~51s** 不等；
`flush()` 连不上时 0.9s。所以：构造放 lifespan + **超时保护**（`LANGFUSE_INIT_TIMEOUT=10s`，
超时就放弃上报）、flush 只在关机、请求路径上只做"建 span 对象"。

### TTFT 埋在哪：三处同源

TTFT **不是**一个孤立数字，是"生成开始 → 首 token"的区间。同一次测量写三处：

| 消费方 | 字段 | 用途 |
|---|---|---|
| SSE `done.stage_ms.first_token` | 相对请求起点 | 前端展示、压测脚本采集 |
| JSONL `turn.metadata.ttft_ms` | 相对请求起点 | 会话回放 |
| Langfuse generation | `completion_start_time` | UI 的 Latency to First Token |
| Prometheus | `ttft_seconds` 直方图 | 告警与 P95 |

另有一个容易搞混的字段：`generate.metadata.first_token_offset_ms` 是**相对生成段起点**
的首字延迟（本例 981ms），而 `ttft_ms` 是相对**请求起点**（1784ms）。
拿前者去比 `span.duration_ms` 会得出"TTFT 比生成还慢"的怪结论 —— 这个坑本项目的
测试用例踩过一次，现在两条断言分开钉。

### 开销实测

```powershell
python -m scripts.bench_trace --rounds 500      # 一条命令重跑，别信文档里的陈年数字
```

口径：**一轮 = 1 turn 根 + 10 个阶段 span + 1 rerank + 2 工具 = 13 行**，
每行同步 `write+flush`（`JsonlSink` 的设计选择），单线程串行 500 轮，对照组走
`NullTurn`（埋点调用一行不少、只是不记录）。本机 Windows 11 / Python 3.12 / NTFS，
**同一条命令独立跑八次**：

| | P50 | P95 | max | 写出 |
|---|---|---|---|---|
| 埋点关（`enabled=False`） | 0.02~0.10ms | 0.03~0.16ms | 0.6ms | 0 |
| 埋点开（本地 JSONL） | **0.64~2.05ms** | **2.4~10.5ms** | 16.4ms | 3.2MB / 500 轮（≈6.5KB/轮） |

八次原始 P50：0.64 / 0.67 / 0.68 / 1.38 / 1.53 / 1.70 / 1.87 / 2.05ms。
折算到一条 3.4s 的请求：**0.02%~0.06%**。

> **为什么给区间而不是一个数**：波动不是代码在变，也**不能简单归因于"机器忙不忙"**——
> 有两次是和 pytest 并行跑的（1.4~2.1ms），但另有两轮机器空着也测到 1.87ms 和 0.67ms。
> 现象是：同一个 500 轮里所有 span 会**一起**偏快或偏慢（同一个新建目录落在杀软扫描窗口内、
> 或 NTFS 日志压力大时整批都慢）。这是推断，不是定论，但足以说明：**单点数字没有意义，
> 要么报区间，要么报分布**。真要压这条长尾，办法是按轮批量 flush（见 §11）。
>
> **数字变了，原因要交代**：早先记的是 P50 0.605ms / 2.4KB 每轮，那是"1 turn + 3 span
> + 短答案"的口径，与真实链路（13 行、几百字答案、带 `rank_scores`/`top_sources`）
> 不可比。换口径就换数字，**不沿用旧值**——沿用一个更漂亮的旧数比没有数更糟。
>
> 真正贵的是 Langfuse 构造（一次性）与 flush（关机），两者都不在请求路径上。

### 接入 Langfuse（可选，15 分钟）

```bash
# .env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com   # 自托管改成 http://<host>:3000
```

然后**先自检再重启**：

```powershell
python -m scripts.trace_doctor              # 配置 / 目录可写 / host 可达 / auth_check
python -m scripts.trace_doctor --send-test  # 真的发一个 span（验证写入链路，不只认证）
```

`--send-test` 是唯一能证明"端到端通了"的动作：`auth_check` 只验认证，
配额、project 配置、网络白名单都可能拦在写入这一层。

> **本项目当前状态（2026-10-18 更新）**：**云端已验证** —— 真实 key 下
> `trace_doctor --send-test` 构造通过（宿主机实测 16.5s，见下方 `LANGFUSE_INIT_TIMEOUT`
> 的说明）、`auth_check` 通过、测试 span 发送成功；容器内发出真实请求后，
> 用 Langfuse v2 API（`/api/public/v2/observations`）核对到 **12 条 observation**：
> 一轮 10 个阶段 span 全部在云端，`embed`→EMBEDDING / `recall`→RETRIEVER /
> `generate`→GENERATION 类型映射正确，`session_id` 正确传递（Sessions 视图可按会话归组）。
> 本地 JSONL 全链路照旧（`selftest --only trace` 16/16）。
>
> 两个如实记录：① **UI 截图没有** —— 验证走的是 API（脚本里没有浏览器），你在
> UI 看到 trace 后这一步才算对你自己闭环；② 容器日志偶发 OTEL
> `Failed to detach context`（15 分钟 2 请求出现 1 次），已确认为 SDK 内部噪音：
> 同一时段云端 12 条 observation 完整，detach 失败不影响上报，已在
> `tracing.py` 里降为 DEBUG 并附证据注释（`tests/test_tracing.py` 有对应锁行为的用例）。
>
> **重要：云端可见性有分钟级延迟**。2026-10-18 实测：请求结束后本地 JSONL 立即全量
> （10 行），但云端先是只有 turn 根 span，**1~3 分钟后 10 条子 span 才陆续出现**
> （batch exporter 的 flush 节奏，非配置错误 —— 容器内最小复现 root+child 两个都能
> 即时 flush 上云）。所以**不要在请求结束后立刻刷新 UI 下结论**；本地回放
> （`trace_replay`）永远是实时的，这也是"本地全量是一等公民"的又一个理由。

### 会话回放（不依赖 Langfuse）

```powershell
python -m scripts.trace_replay --list-sessions           # 有哪些会话
python -m scripts.trace_replay --session sess-6a6b9d39   # 逐轮时间线 + 甘特条
python -m scripts.trace_replay --last 3                  # 最近 3 轮（跨会话）
python -m scripts.trace_replay --trace 970bcce7          # 单轮（用 meta 帧里的 trace_id）
python -m scripts.trace_replay --session xxx --md replay.md   # 导出 markdown 贴文档
```

`meta` 帧里带 `trace_id`（新增字段，老前端会忽略），所以从一次**慢请求**到
一次**可回放**是闭环的：屏幕上看到 trace_id → 直接 `--trace` 或去 Langfuse 搜。

Langfuse 侧靠 `session_id` 归组（`propagate_attributes(session_id=...)` 写在 root span 上），
Sessions 视图里能看到同一会话的多轮。

### 排障口诀

| 现象 | 先看这里 |
|---|---|
| `data/traces/` 空 | `OBSERVABILITY_ENABLED`；容器内 `/app/traces` 是否可写（`/app/data` 是只读的） |
| 有文件但没 `turn` | 请求被客户端提前断开 → 兜底 `finish(status="aborted")` 应补上；若仍缺，查 `Tracer._emit` 的异常日志 |
| 本地有、Langfuse 没有 | `trace_doctor`：key / host / 采样率；401 = key 与 host 不匹配 |
| 启动慢十几秒 | Langfuse 构造在等网络（SDK 5.0.1 实测 10~51s）→ `LANGFUSE_INIT_TIMEOUT` 已兜底，或先 `trace_doctor` |
| 回放里某个阶段标 `!` | `status_message` 给了原因；`rerank:disabled` 是**配置的必然结果**，不是故障 |

---

## 6.y 高可用：熔断 / 重试 / 降级链 · Skill 注册表 · Java MCP Server

### 6.y.1 韧性内核：顺序错了就全错

```
breaker（外层，快速失败）
  └─ retry（内层，只吃"抖动"）
       └─ 真实调用（HTTP / JSON-RPC）
```

- **retry 在内**：一次抖动靠重试吃掉，不能计成熔断失败；
- **breaker 在外**：只有"重试用尽仍失败"才算一个失败信号。反过来会让三次重试
  直接把熔断阈值打满——明明只是抖了一下，熔断却开了；
- **熔断打开后不再重试**：`allow()` 微秒级拒绝，把 3 × 2s 的超时代价压成 0。

重试是**白名单**（`TransientToolError` / 超时 / 连接错误 / 5xx），默认不重试——
"参数错了也重试三次"只会把一次失败变成三次失败加三倍耗时。

熔断器是三态机（closed → open → half_open，半开只放 1 个探测防止恢复雪崩），
实现自研而不用 pybreaker/tenacity：同步包装异步比自研更厚，且拿不到
"这几次尝试算一个熔断失败"的边界；时钟与 sleep 可注入，测试不用真等冷却。

降级有**两个维度，别混为一谈**：

| 维度 | 解决什么 | 实现位置 |
|---|---|---|
| **纵向**：同一工具的多数据源 mcp → cache → mock → 失败 | "这个数据源不健康" | `resilience.run_chain`，每一跳记 `Hop`（outcome / 耗时 / attempts） |
| **横向**：同一意图的多条查询路径 by_no → by_keyword → by_service | "参数没命中但意图还能用别的方式满足" | `skills.py` 的 fallback 步骤 |

只做纵向：MCP 一挂全线退 mock，答案里全是假数据；只做横向：换个查询方式
重试一个已经宕掉的依赖，纯属浪费。**ToolResult.chain 把整条链记进返回值**
——用户看到"工单查不到"，回放里能回答"为什么查不到"。

### 6.y.2 Skill 注册表：声明式的能力目录

- **Tool vs Skill**：Tool 是一次 IO 调用；Skill 是"一个业务意图怎么被满足"
  （定位工单 = 按单号查 → 查不到按关键词捞 → 再不行按服务兜底）。
- **匹配是确定性的**：按 `requires` 命中数降序、同分按声明顺序。不做模糊打分——
  "这次为什么走了 ticket_diagnosis"在线上必须能一句话回答。
- **串行执行 + 整体预算**：`budget_ms` 是整条 skill 的上限，超了带着已有结果返回
  并标 `budget_exhausted`；每一步的参数模板填不满就跳过（不构造空参数去查）。
- **YAML 声明、代码兜底**：Skill 是配置不是代码，运维加一条查询顺序不该改 Python；
  YAML 损坏/为空/步骤缺失都整体退回 `DEFAULT_SKILLS`（部分生效比全不生效更难排查），
  且 WARNING 出声——静默退回会让你以为新配置生效了。

### 6.y.3 Java MCP Server（`java-mcp/`）

Spring Boot 3 + Java 17，单端点 POST `/mcp` 三个 JSON-RPC 2.0 方法
（initialize / tools/list / tools/call），GET `/health` 供探测。
5 个工具与 Python 侧 `TOOL_SPECS` 一一对应（渲染格式逐字段对齐，含 `appendIf` 过滤 null 噪音）。
多阶段 Dockerfile（maven 构建 → jre 运行）。

**X-Chaos 故障注入**（请求头，仅测试/演示用）：`X-Chaos: error=500,times=2` /
`X-Chaos: hang=60000` / `X-Chaos: sleep=1200`，计数器带时间窗重置（防跨请求残留误判）。
这是"故障注入下的请求成功率"这个指标能测的前提——没有注入就没有可复现的故障。

**自检入口：`python -m scripts.mcp_probe`**（19 条断言，含 chaos 三态，可直接进 CI）。
不是"多写一个脚本"，而是因为**手敲 curl 在这台机器上一定会失败**：

```powershell
# PS 5.1 下必然 400（服务端只看到 Required request body is missing）
docker exec ops-agent-java-mcp-1 sh -c 'curl ... -d "{\"jsonrpc\":\"2.0\",...}"'
```

PowerShell 把整段 `sh -c '...'` 当**一个**参数传给 `docker.exe` 时，会用双引号包起来
却不转义内层双引号——内层引号把参数劈开，curl 根本没收到 body。同一命令在 bash 里 200、
在 PS 5.1 里 400，**错误信息指向服务端，实际坏在客户端**。
脚本用 argv 列表 + `--data-binary @-`（body 走 stdin），全程无 shell 拼接。
另外两个坑也一并绕掉：① 宿主机 **8080 是 nginx 的**（统一入口 → FastAPI），
`localhost:8080/mcp` 必然 404 且返回体是 FastAPI 形状的 `{"detail":"Not Found"}`；
java-mcp 的调试端口单独用 `18081:8080`（见 compose 的 `ports`），用 `--url` 指过去即可；
② 服务端耗时必须取 curl 的 `%{time_total}`——Docker Desktop 上 `docker exec` 每跳自付 1~3.4 s，
用墙钟会把 `sleep=400` 的注入淹没在噪声里。

**用 Postman 直连调试**（`18081` 只给本机开，生产/演示流量仍走 nginx 8080）：

| 项 | 值 |
|---|---|
| URL | `http://127.0.0.1:18081/mcp` |
| Method | `POST` |
| Header | `Content-Type: application/json`（可选 `X-Chaos: error=500,times=2`） |
| Body | raw / JSON：`{"jsonrpc":"2.0","id":1,"method":"tools/list"}` |
| 探活 | `GET http://127.0.0.1:18081/health` → `{"status":"ok","tools":5}` |

> 两个 `/health` 都返回 200，**只有响应体能分辨打到了谁**：java-mcp 带 `tools`，
> FastAPI 带 `model`/`prompt_version`。`mcp_probe` 已把这条做成断言
> （`/health 带 tools 字段`），否则打错端口时前面的检查会全绿、只在 RPC 上莫名其妙 404。
> 同理，`{"detail":"Not Found"}` 是 FastAPI 的 404 形状，Spring 的 404 带 `timestamp`/`path`。

### 6.y.4 实测三场景

| 场景 | 注入 | 表现 |
|---|---|---|
| 正常 | 无 | `source=mcp`，一次成功 |
| 抖动 | error=500,times=2 | retry 第 3 次救回，`attempts=3`，`source=mcp` |
| 持续故障 | error=500,times=999 | 重试用尽 → 熔断 closed→open → 后续请求微秒级拒绝 → `source=mock`（chain 里可见 `circuit_open` 跳） |

冷却到期后 half_open 探测成功 → 回 closed，恢复正常。

### 6.y.5 两个已修复的接线 bug（比设计更值得记）

1. **注入的 provider 不该被迫知道熔断器**：测试里的假 MCP 没有 `.breaker` 属性，
   生产代码直接 `mcp.breaker.is_open` 抛 AttributeError——且发生在降级链**之外**，
   把整次调用打挂（mock 兜底都没接到）。注入点上的属性一律 `getattr` 防御。
2. **测试意图要跟着架构走**：skill 命中先于工具计划后，"计划为空"不再等于"没有工具"。
   原 no_plan 测试的句子恰好命中 ticket_diagnosis，tool_call 帧来自 skill 步骤——
   测到的不是它声称的逻辑。修法不是改断言，而是给 registry 换空 skill 注册表，
   把"计划为空"分支真正隔离出来。
3. **"未命中"不等于"降级"**：降级链把缓存未命中记成 `not_ok`，于是健康路径的
   `tool_result` 每条都带 `degraded_from:["cache"]`（实测，见下）——读事件的人只能
   得出"缓存一直是坏的"。缓存 miss 是正常运营事件（新 key 第一次必然 miss），
   单独记 `outcome="miss"`，`degraded_from` 只报真正失败的那几跳：

   ```json
   改前：{"source":"mcp","breaker":"closed","degraded_from":["cache"]}   ← 每条正常调用都长这样
   改后：{"source":"mcp","breaker":"closed"}                             ← 只有真降级才带
   ```


---

## 6.z 五大指标：怎么测、测到了什么（D2 下午）

### 6.z.1 一张表：指标 ↔ 工具 ↔ 口径

| 指标 | 工具 | 口径（必须一起报） |
|---|---|---|
| 系统可用性 | `scripts/fault_drill.py` | **故障注入下的请求成功率**（收到 token 即成功），不是 SLA |
| TTFT | `loadtest/locustfile.py` + `run_eval` | 相对请求起点；缓存命中与否分开看 |
| 并发数 | `loadtest`（staged 10→120 users） | 报告必须带并发数与压测时长 |
| 上下文召回率 | `scripts/eval_matrix.py` + `scripts/run_eval.py` | **文档级**；严格口径（非图谱扩展） |
| 缓存命中率 | `/metrics` → Grafana | hit/(hit+miss)，`rejected` 不算 miss |

### 6.z.2 答案侧评估：RAGAS 口径的确定性实现（`scripts/run_eval.py`）

`ragas` 库在本机装不上（pip 离线）。与其编一组"看起来专业"的分数，不如只实现
**不需要 LLM judge** 的部分，并把口径差异写清楚：

- `context_recall`：**文档级**，与检索评估同口径；
- `context_precision`：文档级 AP（相关文档排得靠前才高分）；
- `anchor_hit_rate`：答案是否包含标注的关键事实（`ORD-429-001`、`5`、`QPS`）。
  **这不是 faithfulness**：它只回答"事实有没有出现"，不回答"是否被 context 支撑"；
- `abstention_ok_rate`：只对"rag/hybrid 且无 golden"的题计（该弃答的题）。

实测（2026-10-18 20:00，52 题 / 48 有 golden，容器镜像 2026-10-17 18:41）：

| 指标 | 值 | n |
|---|---|---|
| context_recall | **0.8785** | 48 |
| context_precision | 0.7841 | 48 |
| anchor_hit_rate | 0.8397 | 52 |
| TTFT p50 | 2298 ms | 52 |
| total p50 / p95 | 4054 / 6323 ms | 52 |
| degraded_rate | 0.0 | 52 |

两个值得说的点：

1. **`context_recall` 与检索评估的 serving 口径 0.8785 逐位相同** —— 两条独立测量
   （检索评估直连召回 vs 答案评估走完整 SSE 链路）给出同一个数，说明评估确实在测
   线上那条路径。这不是巧合能凑出来的。
2. **`degraded_rate = 0`**：`rerank:disabled` 是配置的必然结果，被显式排除在降级之外。
   否则这个指标恒为 1.0，等于没有指标（真实踩过）。

### 6.z.3 故障演练：MTTR 不是"再造一遍故障的时间"

`scripts/fault_drill.py`：多类故障 × N 轮，每轮 `finally` 恢复容器并等健康检查。

```
T1 注入 · T2 告警 firing · T3 连续 3 次收到 token
MTTR = T3 - T2    检测延迟 = T2 - T1    端到端 = T3 - T1
```

三个**实测踩出来的坑**（都已修，这才是这一节真正的价值）：

| 坑 | 表现 | 修法 |
|---|---|---|
| 告警引用了不存在的指标 | 停 Redis 后 `CacheDegraded` 永不 firing → T2 只能回填 T1 → MTTR 变成端到端时间的别名 | 表达式改为 `cache_requests_total{result="error"}`（manager.py 里真实打点的那个） |
| 检测窗口里没有流量 | 错误率类告警靠 `rate(...)` 计算，没有请求就恒为 0，窗口再长也测不到 | 检测循环里**持续发请求**（也更像真实用户） |
| **多 worker 导致计数器分裂** | `uvicorn --workers 2`：每个进程一份 REGISTRY，`/metrics` 只返回命中那个 worker 的计数。实测容器内 3 与 2，Prometheus 只看到 1 → 告警时有时无、曲线像心电图 | 改为 1 worker × N 副本（无状态服务按副本扩；或用 multiprocess 模式） |
| **探针打到了缓存** | `use_cache` 默认 true 时，探测 0.3s 命中旧答案、可用性 100% —— 故障被缓存完全遮住，召回路径根本没被碰到 | 演练探针固定 `use_cache=false`（测冷路径），并在报告里写明 |
| **采样频率低于信号持续时长** | 故障时单次请求 30s，探测循环 30s 才轮询一次告警，而告警在 1 分钟 `rate()` 窗口里只 firing 十几秒 → 完美错过 | 告警检测拆成独立线程（1s 一次），与探测解耦 |

还有一条口径红线：**检测失败时 T2 不做事后回填**。回填让报告有一个漂亮的 MTTR，
但那个数不存在；宁可报 `—`（未检测到）。

**缓存故障的真实伤害是延迟，不是可用性**：实测停 Redis 期间可用性掉到 33%、
单次请求耗时中位 31.8s（基线 ~4s，Redis 连接超时 × 两次重试）；
而停 Qdrant 因为能退到 BM25，可用性仍 100%，只是耗时升到 6.8s。

实测（2026-10-18 20:36~20:44，本机 Docker Desktop，2 副本，各跑 1 轮）：

| 故障 | 可用性 | 检测延迟 | MTTR (T3−T2) | 端到端 | 请求耗时中位 | 解读 |
|---|---|---|---|---|---|---|
| stop-redis | 33.3% | 19.4s | — | — | 31.8s | 无熔断路径，恢复依赖 Redis 本身；每个请求都付两次连接超时 |
| stop-qdrant | 100% | 16.5s | **22.8s** | 39.3s | 6.8s | 退到 BM25 仍能答，**MTTR 描述的是"恢复到无降级"** |

> 单轮不是结论。5 轮要看中位与范围（`--rounds 5`），且要一起报机器条件。

### 6.z.4 Grafana 大盘（`monitoring/grafana/`）

`build_dashboard.py` 生成 `dashboards/ops-agent.json`（27 个面板/行），
覆盖：流量自检、请求成功率、TTFT p50/p95、分阶段 P95、缓存命中率/相似度分布/护栏拒绝/token 节省、
熔断状态、工具来源（mcp/cache/mock）、重试与降级跳数、RAG 评估 gauge、Java MCP 指标。

为什么用生成脚本而不是手写 JSON：**指标名写错时 Grafana 不报错，只画一条空线**，
而"没流量"和"名字写错"在排障时方向相反。`tests/test_dashboard_metrics.py` 会
把表达式里的每个指标名对回 `metrics.py` / Java 源码，并校验 datasource uid。

**"大盘没数据"的三种成因，必须先分清**（三种都表现为空面板）：

| 现象 | 真因 | 处理 |
|---|---|---|
| 整盘空，`⓪ 近 5 分钟请求数` = 0 | 只是没流量（`rate()`=0、分位数为 NaN） | 发一条请求；时间窗拉到有流量的时段 |
| 单个面板永远空 | 指标只定义、没人写（死面板） | 守门断言「每个被引用的指标都有写入点」 |
| 面板 `No data` 且 `抓取目标存活`=0 | 该 job 没被 Prometheus 抓到 | 看 `/api/v1/targets` |
| 整盘空，**但压测刚跑过** | 数据源 uid 不匹配：provisioning 后改的 `uid:` 对**已存在**的数据源不生效，真实 uid 仍是自动生成的 —— 查询全失败，而 noValue 把失败渲染成"无流量" | `POST /api/admin/provisioning/datasources/reload`，然后 `GET /api/datasources` 确认 uid= `prometheus` |

顶行 ⓪ 与"请求成功率"用的是 **`chat_requests_total`**（HTTP 层，含缓存命中与客户端断连）。
不能用 `llm_requests_total` 当请求数：那是 LLM 调用数，语义缓存命中时不发生调用 ——
命中率 75% 的压测里，nginx 明明转发了一千多条请求，顶行却写着"无流量"（实测踩过）。

为此做了三件事：
1. **顶行 ⓪ 流量自检**（近 5 分钟/近 1 小时请求数 + 活动时间线），空盘时先看它；
2. **每个面板都设 `noValue`**：显示"无流量"而不是 Grafana 默认的 "No data" —— 后者与
   "PromQL 写错"完全同形；
3. **去掉比例面板的 `clamp_min(分母, 1e-9)`**：它把 0/0 兜成 0，于是空闲时
   "请求成功率"写着 **0%**，看图的人会以为服务全挂了。现在 0/0 = NaN = "无流量"，
   "没发生"和"发生了且失败"能分开。

**评估产物怎么进大盘**：`scripts/run_eval` 是独立进程，它 set 自己的 REGISTRY 没人抓
（面板会永远空）。所以走 `POST /admin/eval/metrics` 写进 api 进程：

```powershell
python -m scripts.run_eval --out eval_output/answers_full.json --publish
python -m scripts.run_eval --rescore eval_output/answers_full.json --publish   # 只重打分也可刷
```

端点只写"算出来的项"（`None` 直接跳过，不写 0），且比例越界（>1）直接 422 ——
历史踩过 AP 未按文档去重算出 `1.3133`，这种数画到仪表盘上不会报警，只会误导。
**抽样（`--limit`）结果刻意不写大盘**：让 8 题的 0.9 覆盖 48 题的 0.8785 比空着更糟。

gauge 是进程内的，`docker compose up -d api` 一次就清零 —— 于是发布时会同时落盘
`data/traces/eval_metrics.json`，启动时 `restore_eval()` 回填。实测重启后三个格子仍是
`0.8785 / 0.7841 / 0.8237`，不需要重新跑评估。

### 6.z.5 压测（Locust）

```
locust -f loadtest/locustfile.py --host http://localhost:8080 --headless \
  --csv eval_output/load --html eval_output/load.html
```

`StagedShape` 固定在 6 分钟内 10→120 用户，退出时打印缓存分布、路由分布、**按原因拆开的降级**，
以及**分 label 的 TTFT**（`hot` = 缓存命中路径，`cold` = 真实链路）。

两个口径必须守住，否则数字只能吓人：

1. **TTFT 不能混算**。hot 与 cold 是两个分布，混在一起的 P95 是混合物分位数 ——
   25% 的 miss 长尾能把整体 P95 拖到 130s，而这个数既不代表"缓存有多快"，
   也不代表"真实链路有多快"。（实测：同一轮里 hot 命中约 2s、cold 首 token 约 11s。）
2. **配置不等于降级**。`RERANK_ENABLED=false` 以前每道题都发一条 `degraded`，
   于是压测里"降级次数"恰好等于"缓存未命中次数"（72 miss → 72 degraded），
   看着像依赖不稳，其实是配置。现在改成 `notice(code=rerank_disabled)`：
   可见，但不算故障。压测输出按原因拆分，`degraded` 应为 0 或指向真实依赖。

**绝对延迟不可引用**（共享 vCPU 上同一条标尺实测漂 7.5 倍），只报同轮内相对差异与质量指标。
报"最大并发"必须按 `错误率 < 1% 且 TTFT P95 < 3s` 分档，且 hot/cold 分别报。

**多副本 ≠ 真负载均衡，除非 nginx 会重新解析**。nginx 对 upstream 主机名只在启动时解析一次：
实测 50 分钟内 1561 条 `/chat` 全部落在同一副本，另一副本 0 条 —— `--scale api=2` 在
Prometheus 里一眼就能戳穿（只有一个实例有业务指标序列）。`nginx.conf` 已改为
Docker 内嵌 DNS（`resolver 127.0.0.11 valid=5s`）+ 变量 `proxy_pass`：每 5s 重解析、
扩缩容/重建换 IP 都能跟上。验证方法：压测后比对两副本的 `chat_requests_total`。

**已修复的瓶颈（D4，2026-10-20）**：旧图 `node_embed` 排在 `node_cache_lookup` **之前**，
exact 命中也先付一次 BGE 编码。实测（10→120 阶梯，2 副本 × 1 worker）：
`stage_seconds` p50 → `embed_query 6.33s` / `recall 0.50s` / `generate 0.17s`；
对应 `TTFT[hot] P50 6668ms` ≈ `TTFT[cold] P50 6425ms` —— 命中的优势被排队吃干。

**修法**：缓存拆成两段节点 `cache_exact →（未命中）→ embed → cache_semantic`。
exact 查询只是一个 Redis GET，命中请求**根本不进编码**；编码只由未命中请求支付，
其结果同时供语义缓存与向量召回共用（一次编码两处受益）。真机验证（同一台宿主机）：

| 路径 | 修复前 | 修复后 |
|---|---|---|
| exact 命中 TTFT | ≈ cold（P50 6668ms，负载下） | **21–26ms**（单请求实测） |
| 命中请求 trace | 含 `embed` 段 | 只有 `route → cache_exact`（0.55–0.66ms），**无 embed 段** |
| miss 请求 | embed 照付 | 照付（编码供语义缓存+召回共用，未变差） |

守护测试：`tests/test_chat_sse.py::test_exact_cache_hit_skips_embed`
（exact 命中时 `embed_one` 被断言不许调用）；图拓扑 13 节点由
`tests/test_orchestrator_graph.py` 钉住。绝对延迟不可跨环境引用（共享 vCPU），
只报同机相对差异。

---

## 6.aa 安全沙箱：写操作必须人工确认（D3）

前面所有工具都是**只读**的（查工单/资产/告警）。加两个写工具（`restart_service` /
`scale_service`）之后，风险的性质变了：只读工具错了顶多答非所问，写工具错了是
**生产事故**，而调用方是一个会幻觉、会被文档字样带偏、会被用户一句话说动的 LLM。

把模型输出直接变成运维命令的输入，是这套系统唯一不能接受的设计。
所以写工具在执行前要过一道闸门：**策略校验 → 人工确认 → 审计**。

### 四条边界（缺任何一条都能被绕过）

| # | 边界 | 实现位置 | 被绕过的后果 |
|---|---|---|---|
| 1 | **策略先于执行** | `sandbox.validate_call`（参数白名单 + 值白名单） | 让人去确认一条非法命令 = 把风险转给人 |
| 2 | **确认绑定参数** | 参数只从待办记录读，**不从请求体读** | 「确认过 A 却执行了 B」 |
| 3 | **一次性 + 过期** | Redis `GETDEL` + TTL | 同一句"重启"被执行两次 |
| 4 | **留痕** | `data/audit/sandbox.jsonl` + `sandbox_decisions_total` | 「人工确认」变成了弹了个窗 |

第 2 条是整套设计的核心。`POST /chat/confirm` 的请求体**只有 `{token, approve, session_id}`**
—— 没有 `tool`、没有 `args`。要执行什么由服务端从待办记录里读。只要接口留一个 `args` 字段，
"确认了 A 却执行 B"就立刻退化成"前端传错参数"级别的 bug，而这是安全边界，不该有这种 bug。

`app/tools/__init__.py::_sandbox_gate` 是唯一收口：闸门放在 `_make_handler`（所有工具调用的
必经之路），而不是 `node_tools` / `planner` —— 放在上游，早晚会有一条新路径忘记加。

### 一次交互 = 两个请求

```
POST /chat/stream {"message":"帮我重启 order-service"}
  → meta → route(needs_write=true)
  → tool_call(query_alerts_by_service) → tool_result      ← 先读现场
  → tool_call(restart_service) → tool_result(status=pending_confirmation)
  → confirm_required {token, tool, args, risk, expires_in, confirm_hint}
  → done {status:"awaiting_confirmation", pending:[...]}     ← 本轮结束，**不生成答案**
POST /chat/confirm {"token":"...", "approve":true}
  → {"ok":true, "status":"executed", "tool":"restart_service", "simulated":true, "text":...}
```

**复合诉求（既要执行又问知识）走同一套契约，但会先答能答的**：

```
POST /chat/stream {"message":"帮我重启order-service,并查一下oom怎么办"}
  → route(hybrid, needs_write=true)                        ← 分句判知识，写操作没被吞
  → tool_call × 3 → restart_service 挂起
  → token×N 正文（OOM 排查照常回答，开头写明"该操作尚未执行"）
  → done {status:"awaiting_confirmation", pending:[...], route:"hybrid"}
```

判定分界线是**分句**：`重启 X 的步骤是什么`（同句问流程）→ 只读；
`重启 X，并查 OOM 怎么办`（知识在另一分句）→ 照写，但要确认。
这一轮**不写答案缓存**——缓存的是"某个动作正在等人确认"这个瞬时状态。

为什么不把 SSE 连接挂住等人点（10 分钟）：中间任何代理都会掐断（nginx 默认 60s），
进程重启就全丢，多副本还对不上。代价是一次交互两次请求，换来的是它在**多副本 / 重启 / 断连**
下都不丢。图里为此多了一个汇合点 `tools_done` + 终局 `await_confirm`：

```
dispatch ─┬─▶ tools  ─┐
          └─▶ recall ─┴─▶ tools_done ─┬─(无挂起)──────────▶ generate → END
                                       ├─(挂起 + 需要检索)──▶ generate → END
                                       └─(挂起 + 纯写)──────▶ await_confirm → END
```

汇合点必须前移到 `tools_done`：条件边挂在 `tools` 上的话，`recall→generate` 那条边仍会触发
`generate` —— 于是"等人确认"的同时模型已经生成了一段"已为你重启"的答案。这个 bug 只在
**真的有人确认**的场景里出现，平时跑测试永远看不见。

### 演示与验收命令

最省事的是这个交互脚本 —— 它把「拦截 → 审批单 → y/n → 回执 → 审计」串成一条流程，
不用手工复制 token：

```bash
py -3.12 -m scripts.sandbox_demo            # 交互：一行一个问题，挂起后当场 y/n/r
py -3.12 -m scripts.sandbox_demo --auto     # 无人值守跑完四幕（策略拒绝/批准/拒绝/重放 404）
```

想看原始帧、一步步手工做：

```bash
# 1) 挂起（注意看 confirm_required 和 done.status）
py -3.12 -m scripts.stream_chat "帮我重启 order-service" --no-cache --show-frames

# 2) 确认执行（token 来自上一步）
curl -s --noproxy '*' -X POST http://localhost:8080/chat/confirm \
  -H "X-API-Key: $OPS_API_KEY" -H 'Content-Type: application/json' \
  -d '{"token":"<上一步的 token>","approve":true}'
# → status=executed, simulated=true, text 里写着「[模拟执行 · 未真正调用编排接口]」

# 3) 重放同一个 token → 404（凭据一次性）
# 4) 越权参数：直接调工具带 service=payment-gateway → 被策略拒绝，连 token 都不发
# 5) 审计：curl -s --noproxy '*' "http://localhost:8080/admin/sandbox/audit?limit=5" -H "X-API-Key: $OPS_API_KEY"
```

**这两个写工具是 `simulated=True`**：它们不会真的动线上，返回文本里明确写
`[模拟执行 · 未真正调用编排接口]`，事件里带 `simulated: true`。演示时把它讲成
"真重启了"，面试官只要问一句"重启调的是哪个 API"就穿帮 —— 这条和 mock 数据不能
当实时数据讲是同一条红线。

### 口径（三条容易报错的）

1. **挂起不计入降级**。`ok=False` 在这套代码里统一表示"没查到"，会被渲染成"暂时不可用"
   并计入 `degraded_responses_total`。而 pending 既不是失败也不是降级，它是正常流程里的
   一次暂停 —— 混进去会让工具失败率凭空上涨，排障时把"有人在审批"误读成"工具在坏"。
2. **`denied` 与 `rejected` 必须分开**。面板上都表现为"没执行"，但处置完全相反：
   `denied` = 参数越界/动作被禁（有人在探边界，要查是谁在调）；`rejected` = 人主动点了不执行
   （正常流程）。
3. **沙箱关闭 ≠ 放行**。`SANDBOX_ENABLED=false` 时写操作**一律拒绝**（fail-closed）。
   反过来（fail-open）会让"Redis 挂了"变成"绕过审批的后门"，而 Redis 挂恰恰是最常见的故障。
   只读工具完全不受影响，所以这个选择不伤可用性。

顺带一个可观测性问题：沙箱最典型的失效方式是**静默的** —— 写了确认逻辑，但写工具的
`risk` 标成了 `read`，于是每次重启都直接执行、一次确认都没弹过。这条只能靠面板发现：
**有写工具却没有 `pending` 计数，说明闸门没接上**。所以 `sandbox_decisions_total` 上了大盘。

### 面试追问速答

- **"为什么不用 LangGraph 的 `interrupt()`？"** 它依赖 checkpointer（本项目还是 backlog），
  且是进程内暂停 —— 多副本下恢复必须回到同一个进程。落库 + 新请求续跑实现的是同一个语义，
  但不依赖 checkpointer，天然多副本可用。换成 `interrupt()` 是"更好的工程"，不是"更好的语义"。
- **"token 存 Redis，Redis 被攻破怎么办？"** 那它就能改待办记录 —— 所以确认执行前**再校验一次**
  参数策略（`_sandbox_gate` 里那个 `validate_call`），并且策略表在代码里、不在 Redis 里。
  真正的兜底是写工具本身仍需要下游权限（本项目是模拟执行，所以只到这一层）。
- **"并发两个确认同时到会怎样？"** 只有一个 `GETDEL` 成功，另一个拿到 None → 404。
  非原子（GET 再 DEL）会双放行，这条有单测钉住（`test_token_single_use`）。

---

## 7. 48 小时计划

| 阶段 | 任务 | 里程碑 |
|---|---|---|
| ~~D0 今晚~~ ✅ 已完成 | 定场景 · 语料 105 篇 + 20 张截图 · 标注 40 条 QA · 全部校验通过 | **评估集已就绪** |
| ~~D0 追加~~ ✅ 已完成 | 向量库抽象层（Qdrant/Chroma/Local 同契约）· 补 5 篇长文档压测切块 · 抓出并修复 3 个切块器 bug · 标注扩到 52 条 | **基线经同口径复核** |
| ~~D1 上午（前一半）~~ ✅ 已完成 | 语料切块 · 多模态入库 · 对话建 index · BM25 + 图谱 · 召回率评估口径 | **索引产物 + 真实基线** |
| ~~D1 上午（后一半）~~ ✅ 已完成 | 向量召回打通（Qdrant 接入 + 本地暴力扫描对照 + 一致性校验）· 意图路由 · SSE 串通 | **链路跑通** |
| D1 下午（进行中） | ~~双路召回 + RRF 融合~~ ✅ · ~~Rerank~~ ✅ · ~~三层记忆~~ ✅ · ~~LangGraph 编排~~ ✅ | 核心闭环（压测数字待 D2） |
| D1 晚 | Langfuse 接入 · 分段耗时埋点 · 缓存命中率接入 Grafana | 可追踪 |
| D2 上午 | ~~Java MCP Server · Skill 注册表 · 熔断/重试/降级链~~ ✅ 已完成（见 §6.y） | 故障可降级 |
| D2 下午 | RAGAS 评估 · Locust 压测 · 5 轮故障注入 · Grafana 大盘 | 五大指标 |
| D2 晚 | compose 一键起 · README · 3 分钟演示录屏 · 简历段落 | 可交付 |

### 时间不够时的降级顺序（按此砍，不伤主链路）

```
多模态 → 长期记忆 → 横向扩展双副本 → Neo4j 图谱 → Skill 注册表
```

**压死不能动的四块**：主链路（路由 + 多路召回 + Rerank + SSE）、可观测、五大指标、缓存。

---

## 8. D0 任务清单

> **✅ 状态更新（2026-10-16）：本节内容已由脚本自动完成，你无需再手工准备。**
> 语料 110 篇（含 5 篇长文档）+ 截图 20 张 + QA 52 条全部就绪并通过校验，详见 `data/README.md`。
> 保留本节作为方法论记录：如果未来换场景重做语料，流程与此相同。

### 8.1 语料准备（约 1.5 小时）—— ✅ 已完成（由 data/source 同源生成）

目标：**100 篇文本 + 20 张截图**。来源优先级：

1. 你手上任何真实的运维文档 / 操作手册 / 制度文档（最优，有真实术语）
2. 公开的中间件文档切片（MySQL / Redis / Nginx 官方中文文档）
3. 自造：按 `scripts/init_mysql.sql` 里那 5 类故障，各写 10 篇排障指南

放在 `data/raw/`，图片放 `data/raw/images/`。

**验收标准**：随便挑 3 篇，里面得有具体的错误码、参数值、步骤 —— 
全是"请检查网络"这种废话的语料，最后召回率指标会很好看但没有任何意义。

### 8.2 标注 52 条 QA（约 2 小时，**这一晚最不能省**）

参照 `eval/questions.example.json` 的 schema。字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一整数 |
| `question` | ✅ | 用**真实用户会说的话**，不要用书面语（否则测不出改写效果） |
| `intent` | ✅ | `rag` / `tool` / `hybrid` / `chitchat` |
| `golden_doc_ids` | rag/hybrid 必填 | **回答该问题必须命中的文档 id** —— 召回率的分母来源 |
| `golden_ticket_nos` | 可空 | 结构化召回的期望工单号 |
| `expected_tools` | tool 必填 | 期望调用的 MCP 工具名 |
| `answer_anchors` | ✅ | 答案里必须出现的**原文**数字/关键值（如 `["800", "150"]`）。注意是原文措辞，不是转述 |
| `key_points` | ✅ | 期望要点，人工抽检完整性时用 |

**分布要求**（`check_annotation` 会检查）：
- 意图覆盖：rag 占多数、tool / hybrid / chitchat 都要有（当前 45 / 3 / 3 / 1）
- 难度：easy / medium / hard 各占约 1/3

标注完成后必须跑：
```powershell
python -m scripts.check_annotation eval/questions.json
```

**易踩的坑**：`golden_doc_ids` 写错一个，召回率就错一个百分点，
而指标看起来完全正常。所以这 20 分钟校验不能省。

`check_annotation` 现在还会**逐条核对 `answer_anchors` 是否真的出现在 golden 文档正文里**
（140 个锚点、0 误报）。写锚点时最容易犯的不是写错值，而是写成**转述**——
比如把语料里的"副本数下降"记成"副本下降"、把"穿透到 DB"记成"DB 穿透"。
这类锚点人工抽检完全看不出问题，但机器对不上，生成层也会被要求去凑一个不存在的字符串。
实测第一版 40 条里有 6 条属于这种，全部改正后才通过。

### 8.3 环境准备（约 30 分钟）

```powershell
# 1. 把 torch wheel 复制过来，加速首次构建（可选但强烈建议）
Copy-Item "D:\AI学习\cs-agent\torch-*.whl" -Destination "D:\ai-project\ops-agent\vendor\"

# 2. 填 API Key
Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 拉镜像（提前拉，别等到 D1 现场等）
docker pull nginx:latest                 # 本机已有同一个可跳过；alpine 变体弱网下经常拉不动
docker pull redis/redis-stack-server:latest
docker pull qdrant/qdrant:latest
docker pull mysql:8.4
docker pull prom/prometheus:v2.54.1
docker pull grafana/grafana:11.2.0

# 4. 验证缓存逻辑（不需要 Docker，现在就能跑）
python -m tests.test_cache_keys
python -m tests.test_cache_manager
```

### 8.4 Langfuse 二选一（约 15 分钟）

- **方案 A（推荐，省时间）**：用 Langfuse Cloud 免费额度，把 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 填进 `.env`
- **方案 B（私有化叙事更完整）**：clone 官方仓库起自托管，然后把 `LANGFUSE_HOST` 指向它

建议先用 A 跑通，D2 晚有时间再换 B —— 不要在第 0 天晚上陷在 ClickHouse 的容器编排里。

> **现状**：代码已接好（见 §6.x「全链路可观测」），**填上 key 就能上报**，
> 不必再改代码。填完先跑 `python -m scripts.trace_doctor --send-test` 验证，
> 再重启服务 —— 顺序很重要：先自检能避免"改了配置重启两次还没数据"。
>
> 没配 key 也不影响任何功能：本地 `data/traces/*.jsonl` 是**全量**的，
> `python -m scripts.trace_replay --session <id>` 照样能回放。

### 8.5 入库链路（D1 上午，✅ 已完成）

```
data/raw/**.md ──┐
                 ├─► 切块（按二级标题，附上下文头）──┐
截图 PNG ────────┘                                  ├─► 建点 ──► Qdrant
  └─► OCR 原文 ─────┐                              │        └─► BM25 索引
     结构化描述 ────┘                              └─► 注册表（文档元数据 + 图谱边）
```

两条 stage，刻意拆开：

```powershell
# build：只有纯计算，秒级，可离线
python -m scripts.ingest --stage build

# index：embedding 模型 + Qdrant + Redis，需要外部依赖
python -m scripts.ingest --stage index --backend qdrant
python -m scripts.ingest --stage index --backend local    # 不写向量库，只出向量缓存
```

产物都在 `data/index/`：`points.jsonl`（可审计的中间产物）、`bm25.json`、
`registry.json`、`index_manifest.json`、`ingest_report.json`，
以及 `vectors.bin` + `vectors.meta.json`（向量缓存，index 阶段产出）。

#### Redis 的两项硬要求（连错实例会静默失效）

缓存层不是"有个 Redis 就行"。有两项是**硬要求**，不满足时的症状都是
**不报错、只是功能悄悄变差** —— 这类问题排起来最费时间，所以做成显式探测：

| 要求 | 不满足会怎样 |
|---|---|
| **Redis >= 6.0** | 新版客户端（redis-py >= 6）默认走 RESP3，连接时先发 `HELLO 3`。Redis 6.0 以下不认识这个命令，握手直接失败：`ResponseError: unknown command 'HELLO'` |
| **必须带 RediSearch** | `FT.CREATE` / `FT.SEARCH` 全部 unknown command → **语义缓存 L2 整层不可用**。但 L1 精确缓存照常工作，指标上只看到"命中率偏低"，看不出是坏的 |

探测实现在 `app/cache/factory.py::probe_redis`。入库（`--stage index`）时会**先探测再写版本号**，
不通过就判为失败并说明原因，而不是"写进去了"。

**为什么客户端协议固定 RESP2**：除了上面的兼容性，还有一个更隐蔽的原因 ——
RediSearch 在两种协议下的**返回结构不同**，实测同一个 `FT.SEARCH`：

```
RESP2: [total, "key:1", ["f", "hello"], ...]                    ← 扁平数组
RESP3: [total, {"results": [...], "extra_attributes": {...}}]   ← 结构化 map
```

而 `RedisCache._parse_search_reply` 是按扁平数组写的。走 RESP3 时 reply 长度变成 2、
while 循环一次都进不去 → **返回空候选**，语义缓存永远 miss，日志里只有一条 warning。
所以 `REDIS_PROTOCOL=2` 不是保守选择，而是当前解析代码的**正确前提**
（要改 3 必须同时重写解析并补 RESP3 回归测试 —— `tests/test_redis_protocol.py` 已把这条钉住）。

**宿主端口用 16379 而不是 6379**：`docker-compose.yml` 把 `ops-redis` 映射为 `16379:6379`。
原因是本机若装过老版本 Redis（Windows 服务形态），它会占着 `0.0.0.0:6379`；
两个进程同时监听时 `localhost:6379` 会命中那个老实例 —— 于是入库同步失败、语义缓存静默失效，
而同一台机器上其他项目可能正依赖它，直接停掉并不合适。换端口是最省事的隔离方式。
**容器之间仍走 `redis:6379`（服务名），不受此映射影响。**

**`.env` 里的 `localhost` 与容器内的服务名是两套**：`.env` 给宿主机脚本用
（`localhost:16379`），`docker-compose.yml` 则在 `api` / `build-kb` 的 `environment` 里
显式覆盖为 `REDIS_HOST=redis` / `QDRANT_HOST=qdrant`。否则容器内的 `localhost`
指向容器自己，直接连不上。

#### 重跑入库时跳过向量化：`--reuse-vectors`

改完语料重跑 index，最慢的一步是 468 段文本重新过 embedding。加一个参数即可跳过：

```powershell
python -m scripts.ingest --stage index --reuse-vectors
```

**复用的判据是「`chunk_id` 序列 + 文本指纹」双双一致**，两者都写在 `vectors.meta.json` 里：

| 判据 | 为什么需要 |
|---|---|
| `chunk_id` 序列（含顺序） | 增删块、调切块参数都会改变它 |
| `text_sha`（chunk_id 与正文一起哈希） | **只比 id 不够**：把文档里的阈值从 800 改成 1000，块数、id 全都不变，但向量对应的是旧文本 |

后一种情况是这套机制里最危险的一个。如果只比 `chunk_id`，改正文后会静默沿用
**旧向量**，表现是「BM25 查得到新内容、向量路查不到」，而日志一片正常 ——
所以缓存必须带正文指纹，指纹不一致就宁可重算。

三种未命中都会**显式告警**，而不是静默重算（静默重算会让你以为省了时间，其实白等一轮）：

```
向量缓存未命中：chunk 序列已变化（缓存 402 块 → 本次 468 块）→ 本次全量重算
向量缓存是旧格式（缺文本指纹），无法确认正文是否被改过 → 本次保守重算并升级缓存格式
向量缓存未命中：chunk_id 完全相同但正文已修改 → 复用会退回旧语义，本次全量重算
```

两个容易误判的点：**首次入库必然没有缓存**（只提示"落盘后下次即可复用"）；
复用失败时新向量**仍会写回缓存**，所以**第二次跑就能复用**，不需要任何额外操作。
另外复用省的是 embedding，向量库的 upsert 仍会执行（468 点，可忽略）。
判断这次到底复用上没有，看日志那行「复用已有向量缓存（468 × 512，文本指纹一致）」
或报告里的 `embedding.reused_cache`。

**七个设计决策**（每条都能被追问三层）：

1. **一个二级标题一个块，并前置上下文头**。语料里"处置步骤"这种标题单独向量化全是噪声，
   拼成"MySQL 连接池耗尽处置 · 处置步骤"才具备可检索语义。这是投入产出比最高的一处改动。
2. **块长上限倒推自模型上下文（460 字符），不是拍脑袋**。`bge-small-zh-v1.5` 的
   `max_seq_length=512`，超出部分会被 tokenizer **静默截断** —— 不报错、不告警。
   而块尾常常正是"处置步骤""回滚方案"这类关键内容。实测初版有 2 个文本块和
   **全部 20 张截图的 OCR 文本**都超限，后者一截就把最后的"判读"结论丢了。
   现在这条约束由 `EMBED_MAX_CHARS` 常量 + 入库不变式双保险守住。
3. **"关联文档"小节不进向量库，转成图谱边**。这一节全是文档 id 字符串，
   向量化后会和"我要查 xx"类问题产生大量虚假相似。它的真正价值是
   **多跳检索的一跳扩展**，应该走 metadata 而不是走向量相似度。
4. **截图两路入库**：OCR 原文（错误码/堆栈/PromQL 必须逐字精确）+ 结构化描述
   （补 OCR 拿不到的话义，让口语化提问也能召回）。两路都切窗，因为截图文本普遍 500~1300 字符。
5. **OCR / 描述都做成可插拔 provider**。真实 OCR 与多模态 LLM 都需要联网，
   如果链路强依赖它们，"入库是否正确"在离线环境就无法验证 ——
   而不可验证的链路不该写进简历。所以提供 `transcript` 回放 provider
   （截图是浏览器渲染出来的，渲染前的文本就是像素里的文本，等价于 OCR 完美识别的 ground truth）
   与 `rule` 规则描述 provider。**这两个 provider 明确标注为离线回放/降级方案，不冒充真实结果。**
6. **点 id 用 `uuid5(chunk_id)` 派生**，不用随机 uuid。同语料重复入库生成完全相同的 id，
   upsert 幂等，不会产生重复点。随机 uuid 在"重跑一次入库"之后库里就会出现双份数据，极难察觉。
7. **不做整库删除重建**，靠"版本号 +1 → 旧数据自然不可达"，与缓存失效共用同一套机制。
   代价是存储有短暂冗余，收益是**入库过程中不出现空库窗口**。

入库报告里会跑 7 项不变式（块长上限、chunk_id 唯一、点 id 无重复、注册表无悬空引用、
**评估集 golden 全部存在于知识库**……）。最后一条尤其重要：标注悬空会让召回率从根上就是错的，
而指标看起来完全正常。

> **这 7 条决策不是一次写对的。** 补了 5 篇长文档做压力测试之后，
> 又抓出 3 个只在该形态下复现的切块器 bug（表格重叠把数据行搬到表头之前、
> 跨小节合并产生不存在的路径、重叠从表格尾部偷行形成孤立数据行）。
> 详见 §8.7 —— 那一节记录的才是"为什么要长文档"的真正答案。

### 8.6 向量库选型：为什么默认 Qdrant，以及怎么一行切到 Chroma

结论先说：**默认 Qdrant，但把后端做成了配置项，切 Chroma 不改一行业务代码。**

```powershell
python -m scripts.ingest --stage index --backend qdrant    # 默认
python -m scripts.ingest --stage index --backend chroma    # 换后端
python -m scripts.ingest --stage index --backend local     # 不写库，只出向量缓存
python -m scripts.eval_retrieval --backend chroma --sweep  # 评估同样可切
```

选 Qdrant 的理由（都是可验证的工程理由，不是"更流行"）：

| 维度 | Qdrant | Chroma | 本项目为什么在意 |
|---|---|---|---|
| 原生指标端点 | `/metrics` 直接给 Prometheus | 无，需自己包一层 | 可观测性是这个项目的主线之一，不想为向量库单独写采集器 |
| 水平扩展 | 分片 + 副本，有 Java 官方 client | 以单机/嵌入式为主 | "3 副本无状态横向扩展"是简历里的一条，不能连存储层都没法扩 |
| payload 过滤 | 完整表达式（范围 / 集合 / 嵌套） | 只支持等值与 `$in` 的简化子集 | 检索要按 `kb_version`、`modality`、`category` 过滤，这是**正确性**而非优化 |
| 多租户 | 原生 payload 索引 + 分区 | 靠 collection 切分 | D2 的记忆层要按会话隔离 |
| 与 Java 生态 | 官方 Java client | 主要是 Python/JS | 面试岗位是 FDE，Java 侧能接上是加分项 |

那为什么还要接 Chroma？因为**兼容性本身是被测能力**。抽象出
`VectorStore` 协议（`ensure_ready / upsert / search / count / health / aclose`）之后：

1. `test_cross_implementation_consistency` 用同一批数据、同一个过滤条件跑三个后端，
   **断言排序与分数完全一致** —— 三后端的差异全都是"不报错、只让指标悄悄变差"的那种：
   - Chroma 返回余弦**距离**（越小越相似），Qdrant 返回**相似度**。忘了换算排序就整个反过来，而分数看着正常；
   - Chroma 的 metadata 只接受 str/int/float/bool，list 必须序列化，`None` 要丢掉；
   - Chroma 的 `hnsw:space` 默认是 `l2`，不显式设 `cosine` 的话相似度阈值的语义就变了。
2. 想验证"是检索策略的问题还是向量库的问题"时，切一次 backend 就能二分。

> 你上一版 demo 用的就是 Chroma，所以这套抽象的直接收益是：
> **不需要在新的项目里二选一** —— 默认 Qdrant 拿生产特性，Chroma 作为可切换的对照实现，
> 两者行为一致性有测试锁着。要真把默认值改成 Chroma，只需改 `.env` 里的 `VECTOR_BACKEND=chroma`。

### 8.7 长文档语料：为什么补了 5 篇，补出来什么

原有 105 篇语料形态**高度单一**：每篇 800~1800 字符、零个三级标题、最长 section 550 字符、
最长表格 13 行。后果是切块器里四条分支在真实语料上**几乎零覆盖**（只在合成单测里走过）：
超长 section 的窗口切分、超长表格按行拆并重复表头、段落超长时的句子边界硬切、深层标题归属。

补了 5 篇长文档（`data/source/longform.py`），每篇针对一类失败模式：

| doc_id | 形态压力点 |
|---|---|
| `guide-ingest-pipeline-design` | `## → ### → ####` 三层嵌套 + 单 section > 1800 字 → 触发窗口编号 |
| `pm-payment-outage-2026q2` | 22 行时间线表格 → 触发表格拆分与表头重复 |
| `spec-capacity-plan-2026h2` | 12×6 矩阵表格 + 跨章节术语依赖 |
| `guide-db-migration-playbook` | 9 阶段含命令块与校验 SQL → 测代码块跨行切分 |
| `spec-tracing-standard-v2` | 大量"术语：定义"条目 + 采样表 → 测列表项切窗后上下文丢失 |

**收益不是"语料变多了"，而是真实抓出 3 个切块器 bug**（都只在长文档上复现）：

1. **表格重叠把数据行搬到表头之前** —— `spec-tracing-standard-v2` 的某个块以
   `| error | bool | true |` 开头，出现在了表头行之前。修法：当前片段是表格时不取 overlap 尾部。
2. **跨小节合并产生误导性路径** —— 合并块的标题变成 `术语定义(3/3) / Span 属性必填项(1/3)`，
   两段内容挂在一个根本不存在的路径上。修法：合并后路径**退回两个小节的公共前缀**，并剥掉 `(1/3)` 这类窗口标记。
3. **重叠从表格尾部偷行，形成无表头的孤立数据行** —— 某个块以
   `| svc-file-service | 4 | 280 | 700 | 900 | −4% |` 开头，读起来完全无法理解。
   修法：前一片段末尾落在表格行内时不取 overlap。

三个都有回归测试锁住。**如果没补这 5 篇，这三个 bug 会静静地留在索引里** ——
块切错了是不可逆起点，后面所有指标都建立在这上面。

长文档还顺带暴露了同一个"单一事实源"问题的另一面：容量规划表里的副本数是手写的，
和服务手册里的定义有 9 处矛盾。已改为从 `SERVICES` **派生**计算（含余量列），
让矛盾在结构上不可能出现，而不是靠人记得核对。

另外还修了一个隐藏更深的 bug：`corpus_build.py` 的文档 id 引用正则缺左边界，
`svc-search-service` 会让子串 `search-service` 被误判成一个不存在的文档 `arch-service`，
从而生成一条指向空气的图谱边。改成 `(?<![a-z0-9-])` 后消失（不能用 `\b`，
因为 `\b` 把中文当单词字符，会把"见 rb-xxx"这类正常引用也挡掉）。

### 8.8 从零重建全部数据（5 条命令）

`data/raw/` 与 `data/index/` 都在 `.gitignore` 里 —— 它们**全部由脚本生成**，
不提交是刻意的：clone 下来的仓库里，语料的事实来源是 `data/source/*.py`（结构化知识源），
而不是 110 个生成出来的 markdown 文件。

```powershell
python -m scripts.gen_corpus              # 110 篇 Markdown 语料
python -m scripts.gen_screenshots         # 20 张可 OCR 截图（需 Chrome/Edge）
python -m scripts.gen_image_transcripts   # 截图的 OCR 转录（离线回放用）
python -m scripts.gen_eval                # 52 条 QA 标注集（golden 生成时硬校验）
python -m scripts.ingest --stage build    # 建索引产物（468 点 + BM25 + 图谱）
```

改完语料或标注之后，跑这两条校验再谈指标（都离线、秒级）：

```powershell
python -m scripts.check_corpus                       # 语料结构与引用完整性
python -m scripts.check_annotation eval/questions.json # 标注集合规 + 锚点真实性
```

语料改完后重跑向量入库，加 `--reuse-vectors` 跳过 embedding（复用判据见 §8.5）：

```powershell
python -m scripts.ingest --stage index --reuse-vectors
```

---

## 9. 简历段落

> **D2 之后以 `docs/DELIVERY_D2.md` 第二节为准**（含答案侧评估、缓存压测、故障演练的实测数字）。
> 本节保留的是更早的检索/路由/编排证据链，两处数字口径不同，不要混用。

> ⚠️ **本节的规矩：没有实测数字就留空，不许填估计值。**
> "3 副本吞吐提升 4 倍"这种句子，面试官只要追问一句
> *"怎么测的、基线是几个副本、压的是什么接口"* 就会露底。
> 所以下面的 `__` 全部对应 §7 里**尚未执行**的实验，做完一个填一个。

**已经实测、可以现在就写的部分：**

> - 实现 **BM25 + 向量双路召回 + RRF 融合 + CrossEncoder 精排**，
>   自建 52 题标注集（含 12 道长文档专项题）与 19 道对抗性路由用例，
>   context recall@5 由 **0.8646**（BM25 单路基线）提升至 **0.8958**（双路 RRF 融合）；
>   加重排后 **0.9201** —— 但**在用户实际拿到的上下文口径下增益只剩 0.69 pp，
>   低于一题 2.08 pp 的分辨率**。据此得出的结论是
>   **"瓶颈在上下文预算，不在排序模型"**，并因此把重排默认关掉（代价 64.7 s/题）
> - **一个"刻意少一路"的设计**：实时数据（工单 / 资产台账）**不进**向量库，
>   而是走工具层、由意图路由决定是否调用 —— 因为实时数据一旦入了向量库，
>   就会被冻结成一份过期快照。代价是多一层路由，
>   收益是"服务器现在什么状态"这类问题不会拿到三天前的答案
> - 将**评估口径与线上口径收敛到同一份代码**：评测脚本不再自己实现检索，
>   只通过 `RecallConfig` 做参数级消融（通道 / 是否融合 / 池子大小 / 向量索引来源），
>   并用"本地暴力扫描 vs 向量库"的一致性校验（top-5 文档重合率 **1.0000**）
>   证明两条向量路径等价后才引用数字；同时发现并堵上了一个测试盲区 ——
>   `check()` 式断言此前**从未影响过 pytest 退出码**，全红的断言仍会报绿
> - **测量方法学**：用与业务无关的基准（1000×1000 matmul）先标定机器算力，
>   发现同一台机一小时内漂移 **7.5 倍**，据此确立"只引用相对差异与质量指标、
>   绝对延迟只报量级"的报告纪律（并据此撤回了一版错的机器性能结论）
> - 意图路由采用**规则优先**而非 LLM 分类：52 条标注集准确率 **1.0000**、
>   非 rag 类召回 **7/7**、对抗用例 **19/19**，决策带 `reason`/`signals`
>   并可随 SSE 推给前端；明确报告"全判为 rag 的平凡基线 = 0.8654"作为对照
> - 将核心问答链路**重构为 LangGraph 状态机**（10 节点：路由/三层记忆/
>   两级缓存/单飞/工具∥召回扇出并行/生成），以"SSE 事件协议一字不改"为
>   验收标准 —— 端点级用例不改断言全部通过，另加**拓扑守护用例**
>   （扇出改回串行时事件顺序不变、只有延迟变化，端点用例抓不住，拓扑测试能）
> - 实现**三层记忆**：工作记忆 = LangGraph state（请求结束即弃），
>   会话/长期事实外置 Redis（滑窗 24h / 事实 7d），**进程内零会话状态**；
>   事实抽取为规则版并带否定护栏（"我们不用 MySQL"不进事实库 ——
>   一条抽反的事实会被注入后续每轮 prompt，错误半衰期是整个会话）；
>   记忆故障三重防御且降级可见（`stage=memory`）

**仍待实测（对应 §7 的 D1 下午 / D2）：**

> - 设计**意图识别路由 + LangGraph 状态机编排**，将问答 / 工具调用 / 混合类请求
>   分发至不同链路，简单问题跳过重排使 TTFT P50 降低 `__`%
>   → 编排机制**已落地**（10 节点状态机，SSE 事件协议逐帧对齐，见新章节）；
>   仍需：同一批题在"开重排 / 关重排"两种链路下的 TTFT 实测
> - 设计**两级语义缓存**（精确匹配 + 向量 KNN），通过数字锚点一致性护栏
>   规避"16 岁 / 18 岁"类误命中；缓存命中率 `__`%，命中时 TTFT `__`ms
>   → 需要：连问改写问题的命中实验 + TTFT 分位数
> - 基于 **Java 实现 MCP Server** 对接工单系统 / 资产台账 / 用户中心
>   → 当前为 mock provider；真 Server 属 D2
> - 建立**三层记忆机制**（工作 / 会话 / 长期事实），会话状态外置 Redis，
>   支撑无状态横向扩展，3 副本吞吐提升 `__` 倍
>   → 机制**已落地**（Redis 滑窗会话 + 规则抽取长期事实 + 降级可见，
>   端到端用例覆盖注入/隔离/降级）；仍需：Locust 压测，1 副本 vs 3 副本同口径对比
> - 通过**熔断 / 重试 / 降级链**保障依赖故障下的可用性，经 5 轮故障注入实验，
>   平均 MTTR `__`s，故障期请求成功率 `__`%
>   → 需要：`scripts/fault_inject.py` 跑满 5 轮；口径为 MTTR = T3 − T2（见 §6）

> 📌 **一条可以直接讲的方法论**：上面每个 `__` 我都写清了"需要什么实验"而不是
> 一个看似合理的数字。面试时把"我知道它需要怎么测"讲出来，
> 通常比报一个漂亮的数字更让人信。

---

## 10. 演示脚本（3 分钟录屏）

1. **0:00-0:20 场景**：一句业务痛点 + 打开 Grafana 大盘（先给全局印象）
2. **0:20-1:20 能力**：问一个多模态问题（带报错截图）→ 展示检索来源 → 再问一个实时问题（"SRV-0001 什么状态"）触发 MCP 工具调用
3. **1:20-2:00 缓存**：问一个刚问过的问题（语义改写版）→ 展示 `meta` 事件里的 `cache: semantic` 与毫秒级 TTFT → 打 `/admin/kb/bump` → 再问同样问题，重新走真实链路（证明时效性设计有效）
4. **2:00-2:40 高可用**：`dev.ps1 fault stop-qdrant` → 服务仍然回答（降级）→ 恢复
5. **2:40-3:00 指标**：切到指标表（TTFT P50/P95、并发、召回率、MTTR、命中率）+ 一句成本收益

---

## 11. 待补的工程化项（加分但非必需）

- [x] ~~`pytest` 全量接入~~ —— 已完成：**144 个测试函数 + 434 条 `check()` 断言**，
  `check()` 已纳入 pytest 退出码（见 §4 的测试盲区说明）
- [x] ~~全链路可观测~~ —— 已完成（**本地 + 云端均验证**）：10 阶段 span + TTFT 三处同源 +
  `trace_replay` 会话回放 + `trace_doctor` 自检 + `bench_trace` 开销基准（见 §6.x）；
  2026-10-18 真 key 下 v2 API 核对到 12 条 observation，10 阶段 span 全上云
- [ ] **trace 按轮批量 flush**：现在每 span 同步 flush（P50 1.4ms，但 Windows 上 P95
      会到 10ms）；改成 1~10ms 时间窗聚合能把长尾换掉，代价是进程被 `kill -9` 时丢最后一批
- [x] ~~Langfuse 真 key 验证~~ —— 已完成（2026-10-18）：`--send-test` + v2 API 核对；
  剩余小尾巴：在 UI 里亲眼看到 trace 并留一张截图（验证脚本没有浏览器，这步只能人做）
- [ ] `ruff` + `mypy` 静态检查接入 CI
- [ ] LangGraph **checkpointer 接 Redis**（断点续跑 / interrupt 人工确认 / 时间旅行；
      官方 `langgraph-checkpoint-redis` 需联网安装）
- [ ] 长期事实的 **LLM 抽取**（带置信度 + 人工确认），替换规则版 v1
- [ ] 重排候选池的**分批**打分（现在是 1 个 batch 打完整个候选池，
      线上实测约 **29 对/题**、CPU 上是几十秒量级；
      分批 + 早停可能省掉大部分计算）
- [ ] Java MCP Server 的 Spring Boot Actuator 指标接入 Prometheus
- [ ] Grafana dashboard JSON 固化到 `monitoring/grafana/dashboards/`（目前是空目录）
- [ ] 评估指标通过自定义 Collector 暴露给 Prometheus（现在写在 JSON 报告里）
- [ ] 内容安全过滤（企业客户必问）
- [ ] 与 cs-agent 的对比测试：同样 20 题在单文档 RAG 与本平台上的差异
