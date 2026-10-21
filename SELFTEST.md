# D1 阶段自测清单（LangGraph 编排 + 三层记忆 + 双路召回 + 全链路追踪）

> 适用：`D:\ai-project\ops-agent` · Windows PowerShell 5.1 · 约 25~30 分钟
> 判定：每条命令看两处 —— **退出码**（`$LASTEXITCODE`）与 **FAIL 明细**。全过 = 退出码 0。
> 你需要两个窗口：**A 窗口看日志**，**B 窗口敲命令**。
>
> 阶段 7 是可观测（埋点 / 回放 / Langfuse），**不需要 Langfuse key**；
> 阶段 6 与 8.3 部分动作是破坏性的（停 redis / 停容器），按需取用。

---

## 阶段 0 · 准备（约 5 分钟，含镜像构建）

### 0.1 确认自测用配置

```powershell
cd D:\ai-project\ops-agent
Select-String -Path .env -Pattern "^(RERANK_ENABLED|SESSION_MEMORY_ENABLED|SESSION_MAX_TURNS|VECTOR_BACKEND|TOP_K_RETRIEVE|TOP_K_RERANK)"
```

期望：

```
RERANK_ENABLED=false        ← 必须 false，见下方说明
SESSION_MEMORY_ENABLED=true
SESSION_MAX_TURNS=10
VECTOR_BACKEND=qdrant
TOP_K_RETRIEVE=20
TOP_K_RERANK=5
```

- `RERANK_ENABLED` 若为 `true`：单题实测 **64.7 s**（本机 CPU），一轮自测要 10 分钟以上，且首 token 等待可能撞上 `--timeout`。**自测先关掉**，重排单独在阶段 6 验。
  改法：编辑 `.env` 把 `RERANK_ENABLED=true` 改成 `false`。
- 配置改了必须**重启 api 容器**才生效（阶段 0.3 的 `build` 会重建）。

### 0.2 重建镜像（本轮代码改了，必须做）

```powershell
.\scripts\dev.ps1 build
```

- 为什么要 rebuild：`app/pipeline/orchestrator.py`、`app/memory/` 都是新文件，旧镜像里没有。
- 首次会拉依赖，**3~8 分钟**。卡在 `pip install` 属正常。

### 0.3 确认容器健康

A 窗口（保持打开，后面所有阶段都靠它定位问题）：

```powershell
.\scripts\dev.ps1 logs
```

B 窗口：

```powershell
.\scripts\dev.ps1 ps
```

期望看到 **`ops-nginx` / `ops-api` / `ops-redis` / `ops-qdrant` / `ops-mysql` 全部 `Up (healthy)`**，
其中 `ops-api` 有 `start_period 90s`——**刚起时出现 `(health: starting)` 是正常的，等 1~2 分钟**。

#### 0.3.1 必查：模型真的进卷了吗（这里有一个"假健康"陷阱）

`ps` 里的 `healthy` **只代表进程活着**，不代表模型能加载。先看这一条：

```powershell
curl.exe -s http://localhost:8080/ready
```

期望 `"ready":true`，且 `checks.embedding_model.ok` 为 `true`。

若出现 `"ready":false` + `embedding_model.ok=false`，日志里是这两行：

```
No modules.json found for BAAI/bge-small-zh-v1.5, initializing a new SentenceTransformer model.
We couldn't connect to 'https://hf-mirror.com' to load the files, and couldn't find them in the cached files.
```

- **根因**：`huggingface_cache` 命名卷是空的，而 `api` 服务带了 `HF_HUB_OFFLINE=1`
  （刻意离线，避免每次启动去 hf-mirror 撞 429）。`warmup` 的异常被 lifespan 接住，
  容器照常 healthy，模型却始终不可用 —— 一到检索阶段才炸，很难查。
- **处理**（本机已有宿主缓存，零联网，3.4 GB 约 1~3 分钟）：

```powershell
.\scripts\dev.ps1 models          # 宿主 ~/.cache/huggingface/hub → 命名卷
docker compose restart api
curl.exe -s http://localhost:8080/ready     # 期望 ready=true
```

- 若宿主也没有缓存：跑一次 `.\scripts\dev.ps1 kb`（`build-kb` 不设 offline，允许下载），
  模型会顺带落进同一个卷；再 `restart api`。
- **为什么不用"去掉 offline 让 api 自己下"**：hf-mirror 慢且会 429 退避，实测把加载从几十秒拖到几分钟，
  启动期直接顶穿健康检查 —— 离线是正解，前提是卷里先有模型。

### 0.4 冒烟：服务真的能应答

```powershell
curl.exe -s http://localhost:8080/health
```

期望 `{"status":"ok","config_kb_version":1,...}`。

> `/health` 只报**启动时的配置快照**，所以字段叫 `config_kb_version`。
> 运行时版本（Redis 里那个，SSE 的 `meta.kb_version` 用的就是它）在 `/ready` 顶层与
> `checks.cache` 里报。两者打过 `/admin/kb/bump` 之后会分岔 —— 曾经两个接口都叫
> `kb_version`，于是日志里同时出现 1 和 2，看着像 bump 没生效。
若报 **502 且端口上没人监听** → 是宿主机的 `HTTP_PROXY` 把本机请求转走了（本机环境确有 `HTTP_PROXY=http://127.0.0.1:26304`）：

```powershell
$env:NO_PROXY = "localhost,127.0.0.1"
```

（本清单里的 `scripts/selftest.py` 默认直连、已内置处理，无需额外设置；只有手敲 curl 时才需要注意。）

---

## 阶段 1 · 存活与依赖（1 分钟）

### 1.1 自动检查

```powershell
python -m scripts.selftest --only health
```

期望输出（关键 4 行）：

```
[health] /health 与 /ready：区分"进程活着"和"依赖都在"。
  [PASS] GET /health 返回 200
  [PASS] /health 报出 config_kb_version（启动快照）
  [PASS] /health 不冒充运行时版本（字段名不叫 kb_version）
    依赖明细：
      ✓ index             可降级  index_kb_version=1 · 468 块 · BM25=ok
      ✓ embedding_model   critical
      ✓ cache             可降级  kb_version=2（运行时/缓存命名空间）· 索引版本 1 ≠ 缓存版本 2 …
      ...
  [PASS] 索引产物可用（否则问题全答不了）
  [PASS] embedding 模型可用
  [PASS] 缓存探活报出运行时 kb_version（不是配置快照）
```

### 1.2 读判定要点

| 现象 | 含义 | 处理 |
|---|---|---|
| `index` ✗ | `data/index/` 产物缺失或损坏 | `.\scripts\dev.ps1 kb-build` 重建；不行则 `.\scripts\dev.ps1 kb` |
| `embedding_model` ✗ | 容器拿不到模型（`healthy` 仍可能为真） | 见 **0.3.1**：`.\scripts\dev.ps1 models` + `restart api` |
| `qdrant` / `redis` / `tool` ✗ 但仍是 PASS | **设计上的可降级**，不是故障 | 只在明细里看，不用处理 |
| 全部 401 | key 不对 | `--api-key <key>` 显式传入，或用 `.env` 里的值 |

---

## 阶段 2 · 意图路由（2 分钟，不检索不生成）

### 2.1 四类意图自动比对

```powershell
python -m scripts.selftest --only route
```

期望 4 组全部 PASS：

| 句子 | 期望 intent | 说明 |
|---|---|---|
| VPN 连不上怎么办 | `rag` | 知识问答 |
| 查询工单 INC-2026-0412 | `tool` | 需要实时数据 |
| 帮我查工单 INC-2026-0412，顺便说下排查步骤 | `hybrid` | 实时 + 知识 |
| 你好 | `chitchat` | 闲聊短路 |

每条还会验「决策可解释（reason 非空）」；tool / hybrid 额外验 `tool_plan` 非空，
并且**工单号必须完整进到工具参数**（`query_ticket_by_no(ticket_no='INC-2026-0412')`）。

> 工单号格式只能用 `INC-YYYY-NNNN`（正则见 `app/pipeline/intent.py::TICKET_NO`）。
> 用 `TCK-1024` 这类编造格式时，实体抽不到 → 退化成 `query_ticket_by_keyword(keyword='tck')`，
> 而 intent 仍是 tool，用例照样"通过"，真正演示时却答非所问。

### 2.2 手工看决策依据（这是路由的卖点，值得亲眼看一次）

```powershell
cd D:\ai-project\ops-agent
$b = [Text.Encoding]::UTF8.GetBytes('{"message":"帮我查工单 INC-2026-0412，顺便说下排查步骤"}')
Invoke-RestMethod -Uri http://localhost:8080/chat/route -Method Post `
  -Headers @{"X-API-Key" = "demo-key-change-me"} `
  -ContentType "application/json" -Body $b | ConvertTo-Json -Depth 5
```

期望字段：

```json
{
  "type": "route",
  "intent": "hybrid",
  "confidence": 0.85,
  "reason": "既问知识又问实时数据，需两路同时命中",
  "signals": ["ticket_no:INC-2026-0412", "live_verb", "knowledge"],
  "entities": { "ticket_nos": ["INC-2026-0412"], "asset_codes": [], "services": [], "error_codes": [] },
  "needs_rag": true,
  "needs_tool": true,
  "tool_plan": [{ "name": "query_ticket_by_no", "args": { "ticket_no": "INC-2026-0412" } }],
  "tool_plan_summary": "query_ticket_by_no(ticket_no='INC-2026-0412')"
}
```

> 这里用 `[Text.Encoding]::UTF8.GetBytes(...)` 传 body 是**刻意的**：
> PowerShell 5.1 把内联中文参数传给原生程序（含 `curl.exe`）会截断成 `{"message":"VPN`，
> 得到一个看似"服务坏了"的 400。走字节数组则完全绕开这个问题。

---

## 阶段 3 · SSE 全链路（3 分钟，核心）

### 3.1 事件协议自动校验

```powershell
python -m scripts.selftest --only stream
```

期望 13 条全 PASS（含新增的两条协议细节）：

```
  [PASS] 收到帧
  [PASS] meta 是第一帧
  [PASS] 有 route 帧（决策可见）
  [PASS] 有 docs 帧（召回结果进了流）
  [PASS] 有 token 帧（流式生成）
  [PASS] done 是最后一帧数据帧（其后只允许 [DONE] 终结符）
  [PASS] 流以 [DONE] 终结（客户端不会挂住）
  [PASS] 无意外降级（rerank 关闭时的 rerank:disabled 属预期）
  [PASS] docs 里带 sources（可溯源）
  [PASS] 排序键递减（order_by=rrf）
  [PASS] meta 带 kb_version
  [PASS] meta 标注未命中缓存
  [PASS] done 带 total_ms 与分段耗时
    时间线：TTFT x.xxs · token NN 个 · 帧间隔中位 NNms · 生成段 x.xxs
```

三条容易误读的地方（都是本机实测踩过的）：

| 现象 | 不是 bug 的原因 |
|---|---|
| 出现 `degraded` / `rerank:disabled` | 阶段 0.1 要求 `RERANK_ENABLED=false`，**关掉重排必然出现这一帧**。脚本读 .env 判断：关着就只容忍这一条，其他降级仍判失败 |
| `scores` 不递减 | `scores` 是**各通道原始分**：向量 cosine（0~1）与 BM25（无上界，实测 5.98）**不同量纲**，融合用 RRF 只看排名。判排序要看 `order_by` + `rank_scores` |
| 末帧是 `[DONE]` 而不是 `done` | `done` 是最后一个**数据**帧，`[DONE]` 是 HTTP 层的终结符；断言必须区分 |

> 脚本对这一场景固定 `use_cache:false`。原因是缓存命中路径**不推 `docs` / `token`**，
> 不禁用缓存时，第二次跑脚本会因"没有 docs 帧"假失败。

### 3.2 亲眼看一遍流式（可选，但推荐）

A 窗口此时应该有请求日志；B 窗口：

```powershell
cd D:\ai-project\ops-agent
$body = '{"message":"VPN 连不上怎么办","use_cache":false}'
[IO.File]::WriteAllText("$PWD\body.json", $body, (New-Object System.Text.UTF8Encoding $false))
curl.exe -N -X POST http://localhost:8080/chat/stream `
  -H "Content-Type: application/json; charset=utf-8" `
  -H "X-API-Key: demo-key-change-me" `
  --data-binary "@body.json"
```

判定：**帧是一个一个冒出来的**（不是最后一次性刷屏）+ 每帧形如 `data: {"type":"..."}`。
若一次性刷屏 → 中间有代理/缓冲没关（本地直连 nginx，`proxy_buffering off` 已配）。

事件顺序应为：`meta → route → degraded?(rerank:disabled) → docs → token ×N → done → [DONE]`。
tool / hybrid 题在 `docs` 前后会多出 `tool_call` / `tool_result` 帧。

> **终端里"看不出流式"通常不是故障。** 本机实测 deepseek-chat 生成 127 字只花了约 **0.4 s**
> （67 个 token 帧），整段一闪而过 —— 是模型吐字快，不是被缓冲。
> 可测的判据是**时间线**，不是观感：`--only stream` 会打印
> `TTFT 1.8s · 帧间隔中位 6ms · 生成段 0.4s`。若 TTFT 也接近 0、且帧间隔为 0，才是被缓冲。
>
> 想真的眼睛看到逐字：换一个答案长的问题（`VPN 连不上怎么办` 在语料里没有内容，
> 回答是短的"资料未涉及"）——
>
> ```powershell
> .\scripts\dev.ps1 ask "如何排查 502 与 504"
> ```
>
> `ask` 已改成"JSON 落盘 + `--data-binary @file`"，不会再被 PowerShell 吃引号/截断中文。

**要带 `session_id` / 想看紧凑摘要时，用专门脚本**（token 直接连成正文，其余帧压成一行）：

```powershell
cd D:\ai-project\ops-agent
python -m scripts.stream_chat "连接池耗尽的处置步骤是什么" --session demo-01 --no-cache
python -m scripts.stream_chat "那 inode 耗尽呢" --session demo-01          # 同一会话第二轮
python -m scripts.stream_chat "INC-2026-0412 处理到哪了" --session demo-02 --show-frames
```

判定会话生效：第二轮 `[meta]` 里的 `memory.history_turns` 应为 **1**（第一轮是 0）。
`--show-frames` 会逐帧打印原始 JSON，排障时用它；正常看流式回答不要加。
`dev.ps1 ask` 不带 session_id，只适合"看一眼答了什么"。

### 3.3 定位速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 无 `docs` 帧 | 召回为空，或走了缓存 | 确认 `use_cache:false`；再查 `kb_version` 与 qdrant 点数 |
| 出现 `degraded` | 某组件降级 | 先看 `reason`：`rerank:disabled` 是**配置的必然结果**（阶段 0.1 关掉了重排），不算故障；其余看 `stage`：`recall` / `tool` / `memory` / `cache` / `generate` |
| 首帧迟迟不出 | 模型冷启动 | 先做一次 3.2，或调用阶段 3.4 的 warmup |
| 工具类问题一律答"未查询到" | 容器里没有 `/app/eval`，mock 数据为空 | 启动日志出现 `mock 数据不存在：eval/mock_records.json`；`docker-compose.yml` 的 api 已挂 `./eval:/app/eval:ro`，`docker compose up -d api` 重建即可 |
| `done` 不是最后一帧**数据**帧 | 编排顺序被改坏（`done` 之后还有 token/docs） | 回归信号，立刻记下来 |
| 末帧不是 `[DONE]` | 流没正常收尾，客户端会一直挂着等 | 查 chat.py 的 `generator` 是否在 `astream` 之后补了终结符 |

### 3.4 缓存预热（演示/压测前必做，可选）

```powershell
$b = [Text.Encoding]::UTF8.GetBytes('{"questions":["VPN 连不上怎么办","如何申请 VPN 权限"]}')
Invoke-RestMethod -Uri http://localhost:8080/chat/warmup -Method Post `
  -Headers @{"X-API-Key" = "demo-key-change-me"} `
  -ContentType "application/json" -Body $b | ConvertTo-Json
```

期望 `{"warmed":2,"kb_version":1}`。
注意：这会把两条问题写进语义缓存，**之后的"缓存未命中"演示要先 `.\scripts\dev.ps1 stats` / 或清缓存**。

---

## 阶段 4 · 三层记忆（5 分钟）

### 4.1 两轮对话自动校验

```powershell
python -m scripts.selftest --only memory
```

脚本会自己生成一个新 session（打印 `会话 selftest-xxxxxxxx`），然后：

- 第 1 轮发 `我们生产用的是 MySQL 8.0，我是 DBA`
- 第 2 轮发 `我们的数据库要注意什么`

期望：

```
  [PASS] 第一轮历史与事实计数为 0（首次对话的正常行为）
  [PASS] 第二轮 meta 带 memory 统计
  [PASS] history_turns ≥ 1（会话记忆生效）
  [PASS] facts ≥ 1（长期事实抽取生效）
  [PASS] 第二轮无 memory 降级
  [PASS] 第二轮真的有正文（否则下面那条断言无从谈起）
  [PASS] 回答引用了第一轮背景（MySQL）
```

> 「第一轮计数为 0」是**正常行为**不是 bug：那时 Redis 里还没有该会话的历史。
> 注意字段是**在的**（`{"history_turns":0,"facts":0}`）——带着合法 session_id 就一定查过一次
> store，"查过且为空"比"没有字段"信息量更大（后者分不清是没查还是没数据）。
>
> 「回答引用 MySQL」验的是**记忆真的进了 prompt**，而不是只存不用。
> 这条曾经是假用例：脚本按 `text` 字段取正文，而 token 帧的字段叫 `content`，
> 取到空串后 `if answer:` 直接跳过 —— 永远"通过"。现在会先断言正文非空。

### 4.2 手工看 Redis 里到底存了什么

用 4.1 打印出来的那个 session id：

```powershell
docker exec -it ops-redis redis-cli LRANGE ops:sess:<你的sid>:hist 0 -1
docker exec -it ops-redis redis-cli HGETALL ops:sess:<你的sid>:facts
docker exec -it ops-redis redis-cli TTL  ops:sess:<你的sid>:hist
```

期望：

- `hist`：2 条 JSON 字符串（user / assistant 各一条），带 `ts`
- `facts`：若干个 `字段 → 抽取到的原句`，例如 `tech_stack -> 我们生产用的是 MySQL 8.0`
- `TTL`：接近 `86400`（24 h）

### 4.3 跨会话隔离 + 非法 id

```powershell
python -m scripts.selftest --only isolation
```

期望：

```
  [PASS] 新会话的历史与事实计数为 0（跨会话隔离）
  [PASS] 非法 session_id 被静默忽略（不报错、不进记忆）
```

第二条验的是**键注入防护**：白名单 `^[A-Za-z0-9_-]{1,64}$`，`bad id*with:符号` 这类必须被当成"无会话"而不是拼进 Redis 键。

### 4.4 手工复现一次两轮（想亲眼看 SSE 里怎么体现时）

```powershell
cd D:\ai-project\ops-agent
$sid = "u" + (Get-Random -Maximum 99999)
$body = "{`"message`":`"我们生产用的是 MySQL 8.0，我是 DBA`",`"session_id`":`"$sid`",`"use_cache`":false}"
[IO.File]::WriteAllText("$PWD\b1.json", $body, (New-Object System.Text.UTF8Encoding $false))
curl.exe -N -s -X POST http://localhost:8080/chat/stream -H "Content-Type: application/json; charset=utf-8" -H "X-API-Key: demo-key-change-me" --data-binary "@b1.json"

$body2 = "{`"message`":`"我们的数据库要注意什么`",`"session_id`":`"$sid`",`"use_cache`":false}"
[IO.File]::WriteAllText("$PWD\b2.json", $body2, (New-Object System.Text.UTF8Encoding $false))
curl.exe -N -s -X POST http://localhost:8080/chat/stream -H "Content-Type: application/json; charset=utf-8" -H "X-API-Key: demo-key-change-me" --data-binary "@b2.json"
```

第二条的 `meta` 帧里应出现 `"memory":{"history_turns":1,"facts":2}`。

---

## 阶段 5 · 两级缓存与版本号（3 分钟）

### 5.1 命中校验

```powershell
python -m scripts.selftest --only cache
```

期望：

```
  [PASS] 第二遍命中缓存（exact/semantic）
  [PASS] 命中时不再检索（无 docs 帧）
  [PASS] 命中时给出相似度（可解释）
```

判定要点：**命中时没有 `docs` 帧** —— 这才是"短路"，只快一点不算。

### 5.2 看命中统计

```powershell
.\scripts\dev.ps1 stats
```

看 `exact_hits` / `semantic_hits` / `tokens_saved` 在 5.1 之后有增长。

### 5.3 版本号 bump → 全副本即时失效

```powershell
.\scripts\dev.ps1 bump
```

期望 `{"previous":1,"current":2,"note":"全副本缓存已即时失效"}`。

然后立刻再问一次同一问题：

```powershell
python -m scripts.selftest --only cache
```

此时第一遍必然 `miss`（版本号变了），第二遍才命中——**这正好证明"运行时版本号从缓存读、不是配置快照"**。

> 注意：`bump` 只改**运行时缓存里的版本号**，不动 `data/index/kb_version.json`。
> 重启后回到 1。想把 2 落盘，要改 `data/index` 后重跑 `kb-index`。

---

## 阶段 6 · 故障与降级（可选，3 分钟，破坏性）

### 6.1 记忆层挂掉（验证"降级可见且不拖垮回答"）

```powershell
docker stop ops-redis
python -m scripts.selftest --only memory_down
```

期望：

```
  [PASS] 出现 stage=memory 降级事件
  [PASS] 降级后回答仍然完成（done 是末帧）
  [PASS] 降级有可读说明
```

判定意义：**记忆静默失效才是最危险的**——请求照常返回、只是不带历史，用户完全不知道。
这条用例保证 `stage=memory` 会出现在 SSE 里。

恢复：

```powershell
docker start ops-redis
```

### 6.2 重排的代价（只在需要给数字时跑）

```powershell
# 临时打开
(Get-Content .env) -replace "^RERANK_ENABLED=.*", "RERANK_ENABLED=true" | Set-Content .env
.\scripts\dev.ps1 build

# 单题计时（会比较久，属预期）
python -m scripts.selftest --only stream --timeout 300
```

看 `done` 帧的 `stage_ms.rerank`，量级应是**几十秒**。
验完改回 `false` 并 `build` 一次。

### 6.3 工具层熔断 / 降级 / Skill（需要 Docker，java-mcp 在容器里）

前置：`MCP_ENABLED=true`、`MCP_SERVER_URL=http://java-mcp:8080`（`.env` 已配好），
`docker compose up -d java-mcp` 后容器健康。

**正常路**（source 应为 mcp；java-mcp 未启动时降级为 mock 也算通过，但必须标注出来）：

```powershell
python -m scripts.selftest --only tools
```

期望：skill 命中（`tool_call` 带 `skill=ticket_diagnosis`）、`tool_result.source` = `mcp`、
`attempts` ≥ 1。正常路**没有** `degraded_from` —— 只有真降级过才带链，这是刻意的降噪。

**协议 + 故障注入自检**（一条命令，19 条断言）：

```powershell
python -m scripts.mcp_probe          # 默认进容器 curl（chaos 计数器与线上同一份）
```

覆盖 `initialize` / `tools/list`（工具集合与 `TOOL_SPECS` 逐个比对）/ `tools/call` /
未知工具返回 JSON-RPC error / chaos 三态。全 OK 说明 Java 侧协议与注入都正常。

**想从宿主机直连**（Postman / curl 都行，端口见 compose 的 `ports: 18081:8080`）：

```powershell
python -m scripts.mcp_probe --url http://127.0.0.1:18081/mcp
```

Postman 配置：`POST http://127.0.0.1:18081/mcp`，Header `Content-Type: application/json`，
Body → raw → JSON：`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`（想测注入再加
`X-Chaos: error=500,times=2`）。探活 `GET http://127.0.0.1:18081/health` → `{"status":"ok","tools":5}`。

> **别手敲这条 curl**：`docker exec ... sh -c 'curl ... -d "{\"jsonrpc\":...}"'`
> 在 PowerShell 5.1 下必然返回 400（日志是 `Required request body is missing`）——
> 内层双引号把参数劈开，curl 根本没收到 body，**错误指向服务端、实际坏在客户端**。
> 要手敲就把 body 走管道（`--data-binary @-`），不拼引号：
>
> ```powershell
> '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query_ticket_by_no","arguments":{"ticket_no":"INC-2026-0412"}}}' | docker exec -i ops-agent-java-mcp-1 curl -s -X POST http://127.0.0.1:8080/mcp -H 'Content-Type: application/json' -H 'X-Chaos: error=500,times=2' --data-binary '@-'
> ```
>
> 另外 `localhost:8080/mcp` 是**错的**：8080 是 nginx→FastAPI，java-mcp 的宿主端口是 **18081**。
> 区分方法看响应体：`{"detail":"Not Found"}` 是 FastAPI（打错端口），
> 带 `timestamp`/`path` 的 404 才是 Spring。

**持续故障 → 熔断开 → mock 兜底**（故障必须由**应用自己**观察到，所以是停依赖，不是往它发 500）：

```powershell
docker stop ops-agent-java-mcp-1
1..3 | ForEach-Object { python -m scripts.selftest --only tools }   # 阈值 5 次失败，一次调用带 3 次重试，2~3 遍就够
docker start ops-agent-java-mcp-1
```

期望 SSE 的 `tool_result` 帧：

- `source` = `mock`（降级兜底，**不是** mcp）；
- `breaker` = `open`（这次回答发生在熔断期）；
- `degraded_from` 含 `mcp` —— **"为什么这次是 mock 数据"必须能从返回值里读出来**
  （事件流里只带降级跳，完整 chain 在 trace 回放里看）。

**Skill 横向降级**：问"帮我查一下工单 INC-2026-0412 的处理进度"，期望
`tool_call` 帧带 `skill=ticket_diagnosis` 字段（skill 路径没有独立事件类型，
靠这个字段区分），步骤按 按单号查 → 同服务工单 → 关键词兜底 的顺序推
`tool_call`/`tool_result`，前一步查到就跳过后续（trace 回放里 `skipped` 有 `previous_not_empty`）。

判定意义：熔断的价值不是"不挂"，是**挂的时候快**——熔断期一次调用微秒级被拒，
不用每次都付 3 × 超时的代价；且降级路径全程可解释（chain/breaker 字段）。

恢复：`docker compose restart java-mcp`（清掉 chaos 计数），熔断器随冷却自动回 closed。

---

## 阶段 7 · 全链路可观测（3 分钟）

埋点是**旁路**：它不该改变回答，所以它最危险的失败方式不是报错，而是**静默地什么都没记**。
这一阶段验的就是"记下来了、记得全、能倒着还原"。

不需要 Langfuse key —— 本地 `data/traces/*.jsonl` 是全量事实源，云端只是可选上报。

### 7.1 阶段齐全 + TTFT 自动校验

```powershell
python -m scripts.selftest --only trace
```

期望 **16/16**（本机实测，一轮 `如何排查 502 与 504`）：

```
[trace] 全链路追踪：埋点真的落盘了、阶段齐全、TTFT 可查、能按会话回放。
  ✅ 请求正常收尾（埋点没有改变协议）   [实际末帧 _end]
  ✅ meta 帧带 trace_id（凭它就能回放）   [{'type': 'meta', 'cache': 'miss', 'route': 'rag', 'kb_version': 2, ..., 'trace_id': 'ffc9bc3e...'}]
  ✅ trace 已落盘   [10 个 span]
  ✅ 埋到 route / embed / cache_lookup / recall / generate      ← 实际是 5 条独立断言，每条一行
  ✅ turn 收尾并记了 TTFT   [4205.5]
  ✅ TTFT 早于整轮结束（不是把 total 当 TTFT）   [ttft=4205.5 total=5670.97]
  ✅ 生成段记了首字位置（相对 span 起点）   [2670.89]
  ✅ 生成段记了模型名（成本/diff 都要用）   [deepseek-chat]
  ✅ 每行都带同一个 session_id（会话可串起来）
  ✅ turn 记了路由与缓存口径（回放可解释）   [route=rag cache=miss]
  ✅ 回放能按 trace 聚合   [1 组]
  ✅ 回放的时间线可排序（start_ms 齐全）   [缺 start_ms：[]]
```

> 用 `Tee-Object` 之类管道跑时（GBK 码页）`✅` 会降级成 `[PASS]`（见 8.2 的场景表）；
> 直连控制台则原样显示 ✅。断言结论不受影响。
>
> **看断言，不要把数值当基准**：上面是冷启动后首次（TTFT 4.2s）；模型热起来后同一条命令
> 实测 TTFT 935ms / total 2.44s。冷热的差别在 `embed` 与 `generate` 的首次加载，
> 不是埋点不稳定 —— 要报 TTFT 就得连"冷/热"一起说。

一轮正常 RAG 该有的 10 个 span，按时间序：

```
route → memory_load → embed → cache_lookup → singleflight → recall → generate → cache_store → memory_save → turn
```

`turn` 是根，包住其余 9 个；`recall` 内部还有 `rerank` 子 span（仅 `RERANK_ENABLED=true` 时出现），
每个工具调用各有一个 `tools` 子 span。闲聊题会少得多（`route` + `chitchat` + `turn`），那是**对的**。

三条断言值得单独看：

| 断言 | 防的是什么 |
|---|---|
| `meta` 帧带 `trace_id` | 这是**唯一回放入口**。没有它，日志里几十条 span 谁跟谁一组只能靠时间猜 |
| `ttft <= total` | 防"把 `total_ms` 当 TTFT 报"——这种数字很漂亮，一压测就崩 |
| `start_ms` 齐全 | 回放按它排序，缺一条整段错位 |

**TTFT 有两个基准，混用必错**：`ttft_ms` 相对**请求起点**（用户视角等多久），
`first_token_offset_ms` 相对**生成段起点**（生成内部启动开销）。上面这一轮
`ttft=4.21s`、`generate` 段只有 4.06s、首字在生成段第 2.67s —— 前 1.5s 全花在路由和召回上。
拿 `first_token_offset` 去比 `span.duration_ms` 会得出"TTFT 比生成还慢"的怪结论。

### 7.2 会话回放（把一次对话还原成人读得懂的时间线）

```powershell
python -m scripts.trace_replay --list-sessions               # 有哪些会话、各几轮
python -m scripts.trace_replay --session selftest-trace-xxxx # 按会话（多轮按顺序串起来）
python -m scripts.trace_replay --trace ffc9bc3e              # 单轮（trace_id 支持前缀）
python -m scripts.trace_replay --last 3                      # 最近 3 轮
python -m scripts.trace_replay --last 1 --no-gantt --md trace.md   # 贴复盘文档
```

实测输出（就是 7.1 那一轮）：

```
会话回放 · session=selftest-trace-2a491414 · 1 轮
第 19:46:48 轮  trace ffc9bc3e  total 5.67s  TTFT 4.21s  route=rag  cache=miss
  阶段：
     route     start    679ms  dur      1ms       #
     mem_load  start    716ms  dur     74ms       #
     embed     start    797ms  dur    362ms        ###
     cache     start    1.16s  dur      0ms          #
     singlefly start    1.17s  dur      0ms          #
   ! recall    start    1.18s  dur    348ms          ##        <- rerank:disabled
     generate  start    1.53s  dur    4.06s             #############################
     cache_w   start    5.60s  dur     56ms                                         #
     mem_save  start    5.66s  dur      6ms                                         #
  依据：data/raw/faq/faq-05.md, ..., data/raw/commands/cmd-mysql.md
  排序口径：rrf（rank_scores=[0.032787, 0.032258, ...]）
  生成：首字在生成段第 2.67s（占 turn 的 4.21s）
  答案（841 字）：排查 502 与 504 的要点如下：…
```

读法：**甘特条看"谁在占时间"，`依据` 看"答得对不对"**。这一轮 71% 的时间在 `generate`，
前 1.5s 是准备段 —— 想优化首字就动准备段，想优化总时长就动生成段。

> `! recall <- rerank:disabled` 里的 `!` 是**降级标记，不是故障**：阶段 0.1 要求
> `RERANK_ENABLED=false`，关掉重排必然留这一笔。若 `!` 后面的原因是 `recall:xxx` /
> `cache:xxx` / `generate:xxx`，那才要查。

宿主机直接能读，是因为容器写的 `/app/traces` 就是挂载出来的 `./data/traces`（见 `docker-compose.yml`）。
想指别处用 `--dir`。

### 7.3 Langfuse 自检（云端上报，可选）

```powershell
python -m scripts.trace_doctor
```

三段各自回答"配了没 / 写得进吗 / 连得上吗"：

```
[1] 配置
  [OK]  OBSERVABILITY_ENABLED   True
  [OK]  OBSERVABILITY_TRACE_DIR   data/traces
  [OK]  OBSERVABILITY_SAMPLE_RATE   1.0（只影响云端上报，本地永远全量）
  [--]  Langfuse 未配置 key   只落本地 JSONL；要上报请填 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY

[2] 本地落盘
  [OK]  目录可写   D:\ai-project\ops-agent\data\traces
  [OK]  最近文件   2026-10-17.jsonl · 30 条 · 0.3 分钟前
  [OK]  最近一轮   total 5670.97ms · TTFT 4205.5ms · route=rag · cache=miss

[3] Langfuse 连通
  [OK]  host 可达   https://cloud.langfuse.com
  [--]  未启用上报   见第 1 节；本地 JSONL 不受影响
```

配了 key 之后再加 `--send-test`（真发一个 span，比只验连通硬）：

```powershell
# .env 里填 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
docker compose up -d api                     # 配置改了必须重建容器
python -m scripts.trace_doctor --send-test
```

第 3 节应变成认证通过 + 测试 span 已提交；然后到 Langfuse UI 的 **Tracing → Traces**
按 `trace_id` 搜、**Sessions** 按 `session_id` 看多轮是否串在一起。

> **顺序不能反：先本地、后云端。** 云端 401/超时只说明 key 或网络有问题；而本地都没落盘时，
> 去 UI 里翻是白翻 —— 两个故障的排查方向完全不同。
>
> **UI 查不到先等 1~3 分钟再下结论**（2026-10-18 实测）：请求结束本地 JSONL 立即全量，
> 云端却是 batch exporter 慢慢吐 —— turn 根 span 先到、子 span 晚几分钟。这不是丢数据，
> 是上报节奏；本地 `trace_replay` 永远实时。

### 7.4 追踪排障对照

| 现象 | 真因 | 处理 |
|---|---|---|
| `meta` 帧没有 `trace_id` | `OBSERVABILITY_ENABLED=false`（`start_turn` 返回 NullTurn） | 改 `.env` + `docker compose up -d api` |
| `[FAIL] trace 已落盘` | ① 总开关关了；② 容器内 `/app/traces` 不可写 | `docker exec ops-agent-api-1 ls -ld /app/traces`；`docker-compose.yml` 必须有 `./data/traces:/app/traces`（`./data` 那层是 `:ro`，不能复用） |
| 只有 `turn` 一个 span | 路由就短路了（闲聊） | 换 `rag` 类问题再验；闲聊本来就只该有 3 个 span |
| `ttft_ms` 为 `null` | 没产生 token（生成失败；或缓存命中路径没标） | 看 `turn` 的 `status`/`error`；缓存命中也要埋 |
| 回放里时间全是 0 | 某处 span 缺 `start_ms` | 回归信号：说明有人手搓 dict 而没走 `trace.stage()` |
| 启动慢 10s+ 且日志有 `langfuse 初始化未完成` | Langfuse 构造在等网络（本机实测 10~51s） | 属**预期保护**：等 10s 放弃上报、本地照常。想消除就清空 key 或修 host |
| 请求正常但 UI 没数据 | ① 采样率 <1 抽掉了；② key/host 不对；③ **batch exporter 分钟级延迟，别急** | `--send-test` 单独验；采样只作用于云端；等 1~3 分钟再刷 UI |
| `Token was created in a different Context` | span 的 `__enter__`/`__exit__` 跨了 asyncio Context（SSE generator ≠ endpoint） | 已知坑：root 用非 current 版 `start_observation` + 手动 `end()` |

### 7.5 想给"埋点值多少钱"报数（可选，2 分钟）

```powershell
python -m scripts.bench_trace --rounds 500
```

它会跑两组：`enabled=False`（NullTurn 对照）与真写盘，输出 P50/P95/max 与落盘体积。
本机八次 500 轮实测：P50 落在 **0.64~2.05ms**（原始八次：0.64/0.67/0.68/1.38/1.53/1.70/1.87/2.05），
P95 2.4~10.5ms，约 **6.5KB/轮**（13 行）。对 3.4s 的请求是 **0.02%~0.06%**。

> 报数时连**口径**一起报，否则没法比：一轮 = 13 行 span、每行同步 `flush`、单线程串行
> （因此是上界）、不含 Langfuse 网络上报。
>
> 而且**别报单点**：同一条命令反复跑，P50 能从 0.67 跳到 1.87ms（同一批 500 轮的 span
> 会一起偏快/偏慢，最可能是新建目录正好落在杀软扫描窗口）。自己跑一次得到一个数就写进
> 简历/周报，是很容易被一句"你跑了几次"问穿的。

---

## 阶段 8 · 一锅端与收尾（2 分钟）

### 8.1 全场景

```powershell
.\scripts\selftest.ps1
```

默认跑 7 个场景（不含破坏性的 `memory_down`）：
`health, route, stream, memory, isolation, cache, trace`。

```powershell
# 只跑部分场景 / 明确拿到退出码
.\scripts\selftest.ps1 -Only stream
"退出码 = $LASTEXITCODE"
```

**退出码 0 = 本阶段验收通过。**

### 8.2 留证据

```powershell
.\scripts\selftest.ps1 -Log "selftest-D1-$(Get-Date -f yyyyMMdd-HHmm).log"
```

**推荐**用 `selftest.ps1 -Log`（日志由 Python 直接按 UTF-8 写，跟终端编码完全解耦）：

```powershell
.\scripts\selftest.ps1 -Log "selftest-D1-$(Get-Date -f yyyyMMdd-HHmm).log"
```

直接跑也可以（见下方"哪种场景会纠编码"）：

```powershell
python -m scripts.selftest *>&1 | Tee-Object selftest.log   # 管道场景会自动纠编码
# [info] PS5.1/cmd + codepage 936: stdout re-encoded to cp936 (...)
#   [OK] GET /health 返回 200   [HTTP 200]
```

**哪种场景会纠编码，哪种不会**（这条曾被写错，纠正于 trace_doctor 乱码事故）：

| 场景 | Python 写什么 | 屏幕效果 | 脚本动作 |
|---|---|---|---|
| **直连控制台**（直接 `python -m ...`） | 宽字符直写（`WriteConsoleW`），中文**本来就正常** | 正常 | **不动**（动了反而坏，见下） |
| **管道**（`*>&1 \| Tee-Object`）+ PS 5.1 | UTF-8 字节，被按码页 936 解码 → `D1 鑷祴` | 乱码 | 降级到 cp936，正常 |
| `pwsh 7` / `bash` / 重定向到文件 | UTF-8，对方也按 UTF-8 解 | 正常 | 不动 |

> 直连场景那条是**踩出来的**：最早的自愈只看 `sys.stdout.encoding == 'utf-8'`，
> 而宽字符路径下它也报 'utf-8' —— 于是 reconfigure 成 cp936 后，GBK 字节被按 UTF-8
> 解码的控制台接口吃掉，**把正常的中文字成了乱码**（info 行说"已修复"，标题却是坏的，
> 就是这么来的）。现在的判据是底层 raw 的类型（`_WindowsConsoleIO` = 宽字符 = 不动），
> 有三个单测钉着（`tests/test_reconcile_stdout.py`）。

原理（三段缺一不可，值得记）：

| 环节 | 事实 |
|---|---|
| Python 侧 | 环境里有 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，所以写出的是 **UTF-8 字节** |
| 终端侧 | PS 5.1 按 `[Console]::OutputEncoding`（= 码页 936）解码这些字节 → `D1 鑷祴` |
| `Tee-Object` | 拷的是**解码后**的字符串 → 坏的那份被原样落盘，事后回看也救不回来 |

脚本的做法不是"把控制台切到 65001"（PS 5.1 的 `[Console]::OutputEncoding` 是启动快照，
改码页它不跟着变，照样乱），而是**让输出退回到 PS 5.1 期望的编码**：`sys.stdout.reconfigure(encoding="cp936")`。
判定有三层，顺序不能换：

1. **底层 raw 是 `_WindowsConsoleIO`（直连控制台）→ 不动**（宽字符直写本来就正常）；
2. 父进程是 `powershell.exe` / `cmd.exe`（按码页解码）→ 降级到 cp936；
3. 父进程是 `pwsh.exe`（PS 7）/ `bash.exe`（按 UTF-8 解码）→ **保持不动** —— 只按码页判断会把
   pwsh 7 误判成 5.1，那样"修乱码"就成了"制造乱码"；
   取不到父进程 → 不动。

> 降级的代价（**只发生在管道场景**）：GBK 编不出 ✅/✓，脚本会把它们换成 `[OK]` / `[fail]` /
> `[ok]` / `[--]`。直连控制台是宽字符输出，emoji 原样保留。想要管道日志里也有 emoji，
> 就 `chcp 65001`，或用 `selftest.ps1`（它切完会还原）。
>
> `Tee-Object` 产出的日志是 UTF-16 —— VS Code、记事本、`Get-Content` 都能正常打开，
> 只是别拿 `grep`/`type` 直接啃。要干净的 UTF-8 日志就用 `--log`。

### 8.3 收尾

```powershell
Remove-Item body.json,b1.json,b2.json -ErrorAction SilentlyContinue
.\scripts\dev.ps1 down          # 不删数据卷
# .\scripts\dev.ps1 clean       # 若要连向量库/缓存一起清（慎用）
```

> `data/traces/` 不用清 —— 一天一个文件，留着才有"会话回放"可查。
> 真要清就按日期删，别删目录。

---

## 阶段 9 · 五大指标（D2 下午，约 20 分钟，其中 9.3 破坏性）

> 全部在**宿主机**跑（API 走 nginx 的 8080），不要在容器里跑 ——
> 脚本要读 `.env` 的 API_KEY，也要能 `docker stop`。

### 9.1 答案侧评估（先 8 题，再全量）

```powershell
python -m scripts.run_eval --limit 8
```

期望：

- 每题打出 `recall` / `anchor`，**不是全 0**（全 0 先看 docs 帧的 sources 是不是文件路径）；
- `degraded_rate = 0.0` —— `rerank:disabled` 属配置，不该算降级；
- 末行提示 `⚠ limited_sample=true`（抽样结果不能当全量汇报）。

全量（约 4 分钟，52 题会调一次模型）：

```powershell
python -m scripts.run_eval --out eval_output/answers_full.json
```

口径核对：`context_recall` 与 `python -m scripts.eval_matrix --only RRF` 的
serving 口径应**基本一致**（实测都是 0.8785）。两条路径给出同一个数，
说明评估测的是线上那条链路。

只改口径、不重跑模型（例如指标代码修了 bug）：

```powershell
python -m scripts.run_eval --rescore eval_output/answers_full.json
```

### 9.2 Locust 冒烟（1 分钟）

```powershell
locust -f loadtest/locustfile.py --host http://localhost:8080 --headless -u 10 -r 2 -t 60s
```

期望：失败率 0；结束时打印 TTFT 分布、缓存/路由占比。
**不要把这个绝对延迟当基线**（共享 vCPU 会漂），要对比就同轮跑两次。

### 9.3 故障演练（破坏性：会停容器，脚本保证 finally 恢复）

先看计划（不停任何东西）：

```powershell
python -m scripts.fault_drill --dry-run
```

确认列表里是 `ops-redis / ops-qdrant / ops-mysql`。再跑 1 轮验证链路：

```powershell
python -m scripts.fault_drill --faults stop-redis --rounds 1 --run
```

期望与判定：

| 观察 | 含义 |
|---|---|
| `检测延迟` 有值（几秒到十几秒） | 告警真的 firing 了；若一直是 `—`，先看 Prometheus `/api/v1/rules` 与 `cache_requests_total{result="error"}` |
| `可用性 100%` 但 `请求耗时(中位)` 明显大于平时 | **正确结论**：缓存故障伤害的是延迟（实测 ~4s → 32s），不是成功率 |
| `MTTR` 有值 | 恢复时刻按"连续 3 次收到 token"算，不是"容器起来了" |

正式 5 轮（约 15 分钟）：

```powershell
python -m scripts.fault_drill --faults stop-redis,stop-qdrant --rounds 5 --run
```

产物：`eval_output/fault_drill_<时间>.json` 与 `.md`。
**报数只报中位数与范围**，单轮不可引用。

### 9.4 Grafana 大盘

浏览器打开 `http://localhost:3001`（Grafana）→ Dashboards → `Ops Agent` 文件夹 →
`Ops Agent · 主链路大盘`。

**第一步永远看顶行 ⓪**：

- `近 5 分钟请求数` = 0 → 整盘空是**正常**的（rate()=0、分位数为 NaN → 面板显示"无流量"）。
  先发一条请求，或把时间窗拉到有流量的时段（默认 `now-3h`）。
- `近 5 分钟` 有数但 `近 1 小时` = 0 → 刚起来的服务，只是还没跑量。
- `抓取目标存活` 里某 job = 0 → 那个 job 的指标整块缺失，看
  `curl.exe "http://localhost:9090/api/v1/targets?state=active"`。

其余判定要点：

- **面板显示"无流量"而不是 "No data"** 是刻意设计：后者跟"PromQL 写错"同形，没法区分。
- `请求成功率` 空闲时显示"无流量"而不是 0% —— 0/0 不再被 `clamp_min` 兜成 0。
- `熔断器状态` 平时应全是 closed；做 9.3 时能看到 open 的时段。
- `工具结果来源` 里 mock 占比高 = MCP 不健康（不是"工具就是慢"）。
- `上下文召回率 / 精度` 为空 = 没跑过**全量**评估。要它出数：

  ```powershell
  python -m scripts.run_eval --out eval_output/answers_full.json --publish
  # 或对已有产物重打分并刷新（不调模型，快）
  python -m scripts.run_eval --rescore eval_output/answers_full.json --publish
  ```

  抽样（`--limit`）不会写大盘，这是刻意的：面板上只允许一种口径。

### 9.5 Java MCP 指标（可选，需要 java-mcp 起）

```powershell
docker compose up -d java-mcp
docker compose logs -f java-mcp | Select-String "Started McpServerApplication"
```

然后确认 Prometheus 能抓到：

```powershell
curl.exe "http://localhost:9090/api/v1/targets?state=active" | Select-String "java-mcp"
```

期望 `health: "up"`，且大盘的 `Java MCP 工具 QPS/P95` 有曲线。
`/actuator/prometheus` 出现 404 说明镜像还是旧的（本轮的 actuator 依赖没进镜像）。

---

## 阶段 10 · 安全沙箱（D3，约 5 分钟，不需要 Docker）

写工具的失败是**事故**，不是"答得不好"。这一阶段验的是四条边界真的成立，
以及一个最容易漏的点：**写工具没有跳闸**（risk 标成 read → 每次重启都直接执行、
一次确认都没弹过）。后者不会报错，只能靠"有写工具却没有 pending 计数"发现。

### 10.1 离线自测（全部断言，0 网络）

```powershell
py -3.12 -m pytest tests/test_sandbox.py -q -s
```

期望 **18 passed · 95 条 check 全过**。重点看这几条：

```
[OK]   执行的是快照里的服务          ← 带 token 却把参数换成 payment-gateway，仍执行 order-service
[OK]   调用方塞的参数被丢弃
[OK]   第二次被拒                    ← 凭据一次性（GETDEL 原子消费）
[OK]   换工具被拒                    ← 凭据绑定动作，不能挪用
[OK]   服务不在允许清单 → denied      ← 策略先于执行，连 token 都不发
[OK]   沙箱关闭 → 拒绝执行            ← fail-closed，不是放行
[OK]   没有生成任何正文 token         ← 纯写挂起的那一轮不走 LLM
[OK]   复句仍标 needs_write          ← 知识信号只在同一分句里否决写操作
[OK]   同时发了 confirm_required      ← hybrid 挂起，写操作没被知识问题吞掉
[OK]   挂起轮的答案不进缓存            ← 缓存"正在等人确认"会变成假审批
```

### 10.2 交互式体验"挂起 → 确认"（推荐，能一行行看机制）

```powershell
py -3.12 -m scripts.sandbox_demo                 # 交互模式：一行一个问题
py -3.12 -m scripts.sandbox_demo --auto          # 无人值守跑完四幕（录屏用）
```

交互模式里问一句 `帮我重启 order-service`，会看到：

```
[路由] intent=tool needs_write reason=命中写操作动作与服务名，需人工确认后才能执行
[工具] query_alerts_by_service OK source=mcp ...
[工具] restart_service 已拦截 · 等待人工确认
[本轮结束] 没有生成回答 —— 挂起 1 个写操作，等你决策

┌────────────────────────────────────────────────────────────────┐
│  待确认动作（挂起中，尚未执行）                                 │
├────────────────────────────────────────────────────────────────┤
│  动作    restart_service                                        │
│  参数    {"service": "order-service", "reason": "帮我重启 ..."}  │
│  风险    危险                                                   │
│  原因    重启会中断正在处理的请求，属于危险操作                   │
│  执行    模拟执行（不会真的调用编排接口）                         │
│  有效期  599 秒，且只能用一次                                    │
└────────────────────────────────────────────────────────────────┘
你的决定 [y=批准执行 / n=拒绝 / r=批准并验证一次性 / v=看原始帧 / s=跳过] >
```

**复合诉求（一句话里既让重启又问知识）**，再问：

```
帮我重启order-service,并查一下oom怎么办
```

期望：`[路由] intent=hybrid`（`needs_write` 仍为真）、`restart_service` 被拦截、
**OOM 那一段照常生成**（开头写明"该操作尚未执行"）、
`[本轮结束] 已生成 N 字（知识部分已答）—— 另有 1 个写操作挂起`。
判据是两头都不能少：只挂起不答 = 吞掉用户的问题；只答不挂起 = 写操作被静默放过。

按 `y` → 打印回执（`status=executed` / `simulated=true` / 模拟输出正文）；
按 `n` → `status=rejected`，凭据同时作废，**不会执行**；
按 `r` → 先批准，再用同一 token 请求一次 → 必须 `HTTP 404 invalid_grant`。

`--auto` 会把四幕连跑并自带断言：

| 幕 | 问法 | 该看到什么 |
|---|---|---|
| 1 | 帮我重启 payment-gateway | 策略拒绝，**没有审批单**，正文照常生成（对照：不是"凡写必挂"） |
| 2 | 帮我重启 order-service | 挂起 → 直批准 → `executed` + `simulated=true` |
| 3 | 把 order-service 扩容到 5 个副本 | 挂起 → 拒绝 → `rejected`，无执行 |
| 4 | 帮我重启 notify-service | 挂起 → 批准 → 同 token 重放必须 404 |

末尾自动打 `/audit`：`pending / approved / rejected / denied` 四类事件都在，
`store=redis`、`writable=True`。**`writable=False` 说明审计没落盘**（挂载问题，
接口这时会给出 `last_error`，不要当成"没人执行过"）。

### 10.2.1 一步步手工验（不用脚本，看原始帧）

```powershell
py -3.12 -m scripts.stream_chat "帮我重启 order-service" --no-cache --show-frames
```

期望事件序列（**注意最后没有 token**）：

```
meta(cache=miss route=tool) → route(needs_write=true)
  → tool_call(query_alerts_by_service) → tool_result(ok)
  → tool_call(restart_service)         → tool_result(status=pending_confirmation)
  → confirm_required {token, args, risk=dangerous, expires_in, confirm_hint}
  → done {status: awaiting_confirmation, pending:[...]}
```

拿到 token 后确认：

```powershell
$body = '{"token":"<上一步的 token>","approve":true}'
curl.exe -s --noproxy '*' -X POST http://localhost:8080/chat/confirm `
  -H "X-API-Key: $env:OPS_API_KEY" -H "Content-Type: application/json" --data-binary $body
```

期望 `status=executed`、`simulated=true`、`text` 里是
`[模拟执行 · 未真正调用编排接口]`。**再发一次同一个 token → 404**（凭据一次性）。

### 10.3 三条必须成立的拒绝（手工，10 秒）

```powershell
# a) 越权参数：额度外的服务名
curl.exe -s --noproxy '*' -X POST http://localhost:8080/chat/route `
  -H "X-API-Key: $env:OPS_API_KEY" -H "Content-Type: application/json" `
  --data-binary '{"message":"帮我重启 prod-db-primary"}'
# → tool_plan 里**没有** restart_service（没抽出服务名 → 不规划）

# b) 禁止动作：支付服务。走一次 /chat/stream 看 status
py -3.12 -m scripts.stream_chat "帮我重启 payment-gateway" --no-cache --show-frames
# → tool_result(status=denied, error 里带 rb-payment-flow)，**不产生 confirm_required**

# c) 审计与面板
curl.exe -s --noproxy '*' "http://localhost:8080/admin/sandbox/audit?limit=5" -H "X-API-Key: $env:OPS_API_KEY"
# → records 里能看到 pending / approved / denied，token 只有前 8 位
```

Grafana 看 `⑤ 安全沙箱` 那一行：做一次 10.2 应当出现 1 次 `pending` + 1 次 `approved`。
**只有 approved 没有 pending = 闸门被绕过**（写工具的 `risk` 没标）。

---

## 附 A · 全量命令速查

| 目的 | 命令 |
|---|---|
| 起/重建 | `.\scripts\dev.ps1 build` |
| 看状态 | `.\scripts\dev.ps1 ps` |
| 看日志 | `.\scripts\dev.ps1 logs` |
| 重灌知识库 | `.\scripts\dev.ps1 kb` |
| 单场景自测 | `python -m scripts.selftest --only <场景>` |
| 列全部场景 | `python -m scripts.selftest --list` |
| 全场景自测 | `python -m scripts.selftest` |
| 缓存统计 | `.\scripts\dev.ps1 stats` |
| 版本号 +1 | `.\scripts\dev.ps1 bump` |
| 容器内跑 pytest | `.\scripts\dev.ps1 test` |
| 宿主机跑 pytest | `python -m pytest tests -q` |
| 答案侧评估 | `python -m scripts.run_eval --limit 8` |
| 评估产物重打分 | `python -m scripts.run_eval --rescore eval_output/answers_full.json` |
| Java MCP 协议自检 | `python -m scripts.mcp_probe`（加 `--no-chaos` 跳过注入；`--url http://127.0.0.1:18081/mcp` 走宿主端口） |
| 故障演练计划 | `python -m scripts.fault_drill --dry-run` |
| 故障演练 5 轮 | `python -m scripts.fault_drill --rounds 5 --run` |
| 重生成 Grafana 大盘 | `python -m monitoring.grafana.build_dashboard` |
| 校验大盘指标名 | `python -m pytest tests/test_dashboard_metrics.py -q` |
| 会话回放 | `python -m scripts.trace_replay --last 1` |
| 追踪自检 | `python -m scripts.trace_doctor [--send-test]` |
| 埋点开销 | `python -m scripts.bench_trace --rounds 500` |

可用场景：`health` `route` `stream` `tools` `memory` `isolation` `cache` `trace` `memory_down`

> `selftest.ps1` 默认跑前 8 个（含 `tools`）；`memory_down` 是破坏性的（要先停 redis），只能显式 `-Only memory_down`。

## 附 B · 常见错误对照

| 报错 | 真因 | 处理 |
|---|---|---|
| `连不上 http://localhost:8080` | api 没起 / 还在 `starting` / 主机端口被占 | `dev.ps1 ps`；等 90 s；`netstat -ano | findstr :8080` |
| 502（且 8080 无人监听） | 宿主机 `HTTP_PROXY` 劫持本机请求 | `$env:NO_PROXY="localhost,127.0.0.1"`；selftest 默认已直连 |
| 401 `invalid api key` | key 错（`.env` 的 `API_KEY`） | `--api-key <值>` 显式传 |
| 400 + body 被截断成 `{"message":"VPN` | PS 5.1 传中文给原生程序的老坑 | 用本清单的 `UTF8.GetBytes` / `body.json` 方式 |
| `POST /mcp` 返回 400 `Required request body is missing` | **同一个坑的另一副面孔**：`sh -c 'curl ... -d "{\"jsonrpc\":...}"'` 内层引号把参数劈开，curl 没收到 body | `python -m scripts.mcp_probe`；手敲就把 body 走管道 `--data-binary @-`（见 6.3） |
| `POST http://localhost:8080/mcp` 返回 `{"detail":"Not Found"}` | 8080 是 nginx→FastAPI，java-mcp 的宿主端口是 **18081** | Postman/curl 直连 `http://127.0.0.1:18081/mcp`；或 `python -m scripts.mcp_probe` |
| `mcp_probe --url` 全部 `status=0` | 旧版脚本给了 `--url` 仍套 `docker exec`，容器里没这个宿主端口 | 已修（`--url`/`--health` 自动切成直连）；用最新脚本 |
| `[FAIL] 索引产物可用` | `data/index/` 缺产物 | `dev.ps1 kb-build`；再不行 `dev.ps1 kb` |
| Grafana 整盘空 | 窗口内没流量（rate()=0 → 分位数为 NaN） | 先看顶行 ⓪ `近 5 分钟请求数`；发一条请求或把时间窗拉到有流量的时段 |
| Grafana 整盘空，**但压测刚跑过** | 数据源 uid 与大盘引用不一致（provisioning 改 uid 对已存在的数据源**不生效**，uid 仍是自动生成的） | Grafana API：`POST /api/admin/provisioning/datasources/reload`；核对 `GET /api/datasources` 的 uid 必须是 `prometheus`。注意 noValue 会把"查询失败"渲染成"无流量"，别被骗 |
| 顶行显示"无流量"但 nginx 刚转发上千条 | 旧顶行用的 `llm_requests_total` 是 **LLM 调用数**：缓存命中不计入，命中率 75% 时顶行必然误报 | 已加 `chat_requests_total`（HTTP 层，含命中/断连），顶行与成功率面板已切过去 |
| `--scale api=N` 后只有一个副本有流量 | nginx 对 upstream 主机名**只在启动时解析一次**，之后永远打向那个 IP | nginx.conf 已改 Docker DNS resolver（127.0.0.11）+ 变量 proxy_pass，5s 重解析；验证：`docker logs` 两副本的 `chat/stream` 行数应接近 |
| 单个面板永远空 | 指标只定义没写入（死面板） | `pytest tests/test_dashboard_metrics.py`，「每个被引用的指标都有写入点」会点名 |
| 请求成功率显示 0% 但服务正常 | 0/0 被 `clamp_min` 兜成 0（旧口径） | 已移除；现在显示"无流量" |
| 压测"降级次数"= "miss 次数" | 关掉重排时每题发一条 `degraded(rerank:disabled)`（旧口径） | 已改为 `notice(rerank_disabled)`；升级镜像后应为 0 |
| 压测 TTFT P95 上百秒 | hot/cold 混算的混合物分位数，被 25% miss 长尾拖高 | 看分 label 的 `TTFT[hot]` / `TTFT[cold]` |
| `[FAIL] 有 docs 帧` | 走了缓存 | 该场景必须 `use_cache:false`（脚本已固定） |
| `[FAIL] 第二轮 meta 带 memory` | `SESSION_MEMORY_ENABLED=false` 或 redis 不通 | 改 `.env` + `build`；`docker exec ops-redis redis-cli ping` |
| 首 token 30 s+ | `RERANK_ENABLED=true` | 改 `false` + `build` |
| `[FAIL] trace 已落盘` | 埋点关了 / `/app/traces` 不可写 | 见 **7.4** |
| `ModuleNotFoundError: httpx` | 宿主机默认 `python` 不是装了依赖的那个 | 用 `.\scripts\selftest.ps1`（它会自己挑解释器），或 `-Python <路径>` |

## 附 C · 本轮改了什么（自测要覆盖的点从哪来）

| 改动 | 自测阶段 |
|---|---|
| 编排从手写 generator 改为 LangGraph 10 节点状态机 | 阶段 3（事件协议必须一字不变） |
| tools ∥ recall 改为图扇出并行 | 阶段 3（事件顺序不变，延迟应短于两路相加） |
| 三层记忆（会话滑窗 + 规则事实，外置 Redis） | 阶段 4 |
| 记忆降级可见（`stage=memory`，补发在 meta 之后） | 阶段 4.1 / 6.1 |
| `dev.ps1` 修复（原先 `$compose` 当命令名，解析期就报错、整个脚本跑不起来） | 阶段 0 全部命令 |
| `dev.ps1` 加 `selftest` 入口、默认问题改成运维场景、加 UTF-8 BOM | 阶段 0.1 / 8.1 |
| 新增 `scripts/mcp_probe.py`（协议 + chaos 自检，绕开 PowerShell 引号） | 阶段 6.3 |
| `degraded_from` 不再把缓存未命中算成降级 | 阶段 6.3（正常路应**没有**该字段） |
| 修掉 `selftest --only tools` 里一条恒不执行的断言语（判据改用 `breaker`） | 阶段 6.3（停 java-mcp 时应看到 6/6 且报 `degraded_from:["mcp"]`） |
| 全链路埋点（10 阶段 span + TTFT 三处同源 + `meta.trace_id`） | 阶段 7.1（协议不变 + 阶段齐全） |
| 会话回放 `trace_replay` / 追踪自检 `trace_doctor` | 阶段 7.2 / 7.3 |
| 埋点开销基准 `bench_trace` | 阶段 7.5（报数必须带口径） |
| 编码自愈的直连盲区修复（宽字符路径不许 reconfigure） | `tests/test_reconcile_stdout.py` 3 条 |
| 安全沙箱（风险分级 + 参数白名单 + 一次性凭据 + 审计） | 阶段 10（四条边界 + "写工具没跳闸"） |
| 图新增汇合点 `tools_done` 与终局 `await_confirm` | `tests/test_orchestrator_graph.py`（拓扑断言已同步） |
