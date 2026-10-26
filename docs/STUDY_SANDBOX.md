# 安全沙箱怎么读——90 分钟，按"一次写请求的旅行"走

> 前提：图结构和工具层你已经读过，所以这里**不重讲 LangGraph 怎么跑**，
> 只讲「多出来的那一层」：一条写操作从被认出来、到被拦住、到人确认、到真正执行，
> 中间经过哪几个函数，以及每个函数为什么长成那样。
>
> 沙箱横跨 7 个文件、合计 3801 行，但**真正要精读的只有 500 行左右**：
> `app/tools/sandbox.py`（机制主体）、`app/tools/__init__.py`（闸门）、
> `app/pipeline/orchestrator.py` 的三处（改道 / 挂起 / 生成）、`app/api/chat.py`（确认端点），
> 再加 `intent.py` / `planner.py` 的判定源头。其余是注释——本项目注释密度高，
> 注释本身就是设计说明，值得读。
>
> 行号会随代码演进漂移。文中的行号是按当前版本核对过的；
> 如果你读到的内容和标注对不上，先按函数名搜，不要按行号找。

---

## 零、读完的标准：能做到这七条

合上代码，你能讲出来就行，讲不出来就回去读对应 Block：

1. 说出**闸门在哪一个函数里**，以及为什么不能放在 `node_tools` / `planner`；
2. 说出「确认接口为什么不接受 `args`」，以及不接受之后**哪种事故在结构上不可能发生**；
3. 说出 token 的三个属性（一次性、有 TTL、绑定动作），各自靠什么实现；
4. 说出挂起时这一轮 SSE **为什么必须结束**，以及不结束会坏在哪；
5. 说出沙箱挂了 / 策略表里没这个工具时，写操作会怎样，读操作为什么不受影响；
6. 说出「重启 X 的步骤是什么」和「重启 X，并查 OOM 怎么办」**判定的分界线在哪**；
7. 打开 `data/audit/sandbox.jsonl`，指出 `pending → approved` 两行里哪个字段能对上同一次动作。

第 6 条是这套东西里最容易讲错的一条：它是"要不要审批"的判定源头，
判宽了会让人批准他根本没想做的事，判窄了会让写操作**静默消失**。

---

## 一、Block 0 · 先看现象，再看代码（10 min）

```powershell
py -3.12 -m scripts.sandbox_demo --auto      # 四幕连跑，每幕带自动断言
```

看完记四件事，它们决定了后面每个函数存在的理由：

| 现象 | 说明什么 |
|---|---|
| 挂起那轮**没有正文**，`done.status=awaiting_confirmation` | 这一轮不走 LLM。让模型"总结一次还没发生的执行"只会产出编造话术 |
| 审批是**另一个请求**（`POST /chat/confirm`） | 待办必须落库，不能挂在连接上等 |
| 同一 token 第二次 → 404 | 凭据是**消费型**的，不是查询型的 |
| 策略拒绝那幕**照样有正文**（"payment-gateway 不能重启，理由见 rb-payment-flow"） | 拒绝发生在执行前，不影响这一轮回答问题——**不是"凡写必挂"** |

然后再单独试一句**复合诉求**（`--auto` 不跑这一幕，因为它有两个正确输出）：

```powershell
py -3.12 -m scripts.sandbox_demo
帮我重启order-service,并查一下oom怎么办
```

期望：`route=hybrid` + `restart_service` 被拦 + **OOM 那一段照常答** +
`[本轮结束] 已生成 N 字（知识部分已答）—— 另有 1 个写操作挂起`。
两头少一个都是 bug：只挂起不答 = 吞掉用户的问题；只答不挂起 = 写操作被静默放过。

最后打开 `data/audit/sandbox.jsonl` 看一眼真实记录。审计是这套机制的"事后唯一凭据"，
先看它长什么样，后面读代码时你会一直对照它。

---

## 二、Block 1 · 写请求怎么被认出来并规划（20 min）

先解决"谁决定这是一个写操作"，因为**这一步判断错了，后面全白搭**。

| 读什么 | 这段在干什么 |
|---|---|
| `app/pipeline/intent.py:55-64` `SERVICE_NAME` | 服务名抽取。注意前后是**否定环视**而不是 `\b`：Python 3 把汉字算 `\w`，"重启order-service" 里没有单词边界，用 `\b` 会整条失配 |
| `app/pipeline/intent.py:119-128` `WRITE_ACTION` | 只有动词，没有宾语。上面那段注释解释了为什么**必须配服务名**才算写操作 |
| `app/pipeline/intent.py:130-144` `REPLICAS` / `replicas_of` | 抽目标副本数。抽不到就**不规划**——不猜默认值 |
| `app/pipeline/intent.py:183-198` `CLAUSE_SPLIT` / `_write_clause` | 取**写动词所在的那个分句**。整个判定只在这一个分句里看知识信号 |
| `app/pipeline/intent.py:351-376` | 判定本体：`write_hit = 动词 and 服务名`，再算 `asking_how`，最后按 `knowledge` 分成 `tool` / `hybrid`，两个分支都带 `needs_write=True` |
| `app/tools/planner.py:83-110` `_write_calls` | 动作 → 工具名 + 参数。看 `_REASON_CLEAN` 先把危险字符洗掉再当 `reason` |
| `app/tools/planner.py:153-192` `plan` | 写操作排在**最后**：先读现场（告警/工单），再改 |
| `app/tools/providers.py:92-127` `WRITE_TOOL_SPECS` | 为什么写工具不并进 `TOOL_SPECS`（`mcp_probe` 断言"Java 侧必须实现 5 个工具"） |

**判定的三条边界（每条都能被追问）**

```
① 有动词、无服务名        → 不是写操作     "Pod 反复重启该看哪个文档" 是在描述现象
② 有动词、有服务名、同句问知识 → 不是写操作  "重启 X 的步骤是什么" 是在问流程
③ 有动词、有服务名、知识在别的分句 → 是写操作 "重启 X，并查 OOM 怎么办" 是两件事
```

②③ 的分界就是 `_write_clause`。**早期版本用整句 knowledge 一票否决**，后果是 ③
被静默吞掉：不挂起、不拒绝，答案还照常教你怎么重启——用户以为在走审批流程。
这类**漏判比误判危险得多**：误判只是多一次审批（可恢复），漏判是"该拦的没拦"。

**必须能回答**

1. 「Pod 反复重启应该按哪个文档处理」为什么**不**触发写操作？
   → 动词有、服务名没有。语法上它是在描述现象，不是在指名一个对象。
2. 「把 order-service 扩容一下」为什么不规划 `scale_service`？
   → `replicas_of` 返回 None。没有目标副本数时，任何默认值都是事故。
3. 「重启 X，并查 OOM 怎么办」为什么既给出答案又挂起？
   → 路由是 `hybrid + needs_write=True`；图走 `pending 且 needs_rag → generate`，
   答案里"读"的部分正常写完，`done` 自己带 `status=awaiting_confirmation`。
4. `restart_service` 的 `reason` 为什么要过一遍 `_REASON_CLEAN`？
   → 它会被原样写进执行记录和审计。用户原话里可能带 `;` `|` `$()`，
   策略层的 `ArgRule` 也会挡，但不能指望下游每次都对。

---

## 三、Block 2 · 闸门：整件事的核心（25 min，只读 60 行）

**先读 `app/tools/__init__.py:246-285` `_sandbox_gate`**，这是全项目最该背下来的一段。

它的签名返回**两个值**：`(拦截结果, 本次真正要用的参数)`。第二项是整套设计的关键：

```python
if token:                                  # 确认执行路径
    action, why = await sandbox.grant(token, tool)
    if action is None:
        return _denied_result(tool, args, why), args
    ok, reason = validate_call(tool, action.args)   # 用快照再校验一次
    ...
    return None, dict(action.args)         # ← 调用方传进来的 kwargs 被整体丢弃
```

`return None, dict(action.args)` 这一行就是"确认过 A 却执行了 B"不可能发生的原因——
不是因为校验得严，而是**没有第二条数据通路**。调用方（甚至前端）传什么参数进来都没用，
执行用的永远是从 Redis 快照里读出来的那份。

接着读 `app/tools/__init__.py:288-322`：闸门**装在哪**。

```python
async def handler(**kwargs):
    confirm_token = kwargs.pop("_confirm", None) if gated else None
    if gated:
        blocked, kwargs = await _sandbox_gate(...)
        if blocked is not None:
            return blocked
    parts = _tool_cache_parts(tool_name, kwargs)   # 注意：在闸门之后
```

三个细节，每个都能被追问：

- **为什么在 `_make_handler` 而不是 `node_tools`**：工具调用只有这一个收口（`registry.call`
  → handler）。放上游，早晚会有一条新路径（skill / 未来的 ReAct 循环 / 直接调用）忘记加。
- **`_confirm` 用 `pop` 而不是当普通参数**：它是凭据，不是给 provider 的入参。
  留在 kwargs 里会被转发给 MCP / mock，还会进缓存 key。
- **`parts = _tool_cache_parts(...)` 在闸门之后**：参数被换成快照之后才算缓存键，
  否则"用户原来那句话"和"实际执行的参数"会算出两个 key。

再看 `app/tools/__init__.py:347-352`：写工具**不接缓存**（也不接 MCP）。

```python
steps = []
if cache is not None and not gated:   # 写工具不查缓存
    steps.append(("cache", from_cache))
if mcp is not None:
    steps.append(("mcp", from_mcp))
```

两个理由，都要能说：

1. MCP 侧没有写接口（也不该有），转发过去只会得到"工具不存在"；
2. 缓存一个"已重启"的结果 30 秒，等于**把一次动作伪装成可重复查询的事实**——
   用户再问一次会看到"重启成功"，但这次根本没有任何审批发生。

最后读 `app/tools/sandbox.py:167-193` `validate_call` 与 `:525-534` `precheck`：

- 参数白名单：`extra = set(args) - set(pol.rules)`，**没声明的键直接拒绝**（不是忽略）；
- `ArgRule.check` 用 `fullmatch` 不用 `search`——漏一个 `^…$` 就等于允许 `order-service; rm -rf /`；
- `choices` 值白名单优先于正则：正则只能管"长得像不像服务名"，白名单能管"这个服务存不存在"；
- `precheck` 里 `risk == read` 直接 allow，**只读永不过闸**——审批疲劳是最常见的失效方式，
  而且失效时没有任何信号。

**必须能回答**

1. 攻击：拿到 token 后把参数改成 `payment-gateway` 会怎样？
   → 请求体里根本没有 args 这个入口；即便绕过前端直接调 `registry.call(..., _confirm=token)`，
   执行用的是快照，且 `validate_call(action.args)` 会二次校验（策略可能在挂起期间被改过）。
2. 为什么"挂起"和"确认"之间要**再校验一次**？
   → 策略表可能在中间被改（比如刚把某服务加进 `FORBIDDEN_ACTIONS`）。
   校验对象必须是真正要执行的那份参数，不是请求里的。
3. 强制把 `risk` 标成 `read` 会怎样？怎么发现？
   → 闸门被绕过，直接执行。只能靠 `sandbox_write_tools` gauge 与 `pending` 计数对照发现
   （`app/observability/metrics.py:185-190`）。

---

## 四、Block 3 · 凭据与存储（20 min）

读 `app/tools/sandbox.py`，按这个顺序：

| 行 | 内容 | 要抓住的点 |
|---|---|---|
| `200-266` | `PendingAction` | `args` 是**已渲染好的快照**；`digest` 是工具名+参数的指纹，只用于去重与对账，**不用于放行判断** |
| `298-312` | `ActionStore.put` | 同会话**同工具同参数**复用同一个 token。否则模型把同一动作规划两遍，用户要点两次一模一样的确权 |
| `315-322` | `peek` | 不消费。`/chat/confirm` 先 peek 是为了做"这个 token 属于谁"的检查 |
| `324-339` | `consume` | `_take` = Redis `GETDEL`。注释里写着为什么不能 GET 再 DEL：并发下会**双放行** |
| `346-402` | `_get/_set/_take/_del` | Redis 优先、内存兜底；**每个方法都吞异常并记日志**——存储故障不能让工具层 500 |
| `474-491` | `call_context` | 会话 id 走 ContextVar，**不进工具参数**：参数是给 LLM 看的，它能被编造、被套问 |
| `566-582` | `grant` | 工具名对不上要**先删记录再返回错误**，否则 token 变成一个可以反复试探的预言机 |
| `584-606` | `reject` / `approved` / `denied` | 四个事件对称落审计 + 打指标 |

**必须能回答**

1. token 重放为什么一定失败？**两个请求同时确认**同一个 token 呢？
   → `GETDEL` 是原子的，只有一个请求能取到值；另一个拿到 None → 404。
   换成"先 GET 判断有效、再 DEL"就会双放行。
2. Redis 挂了会怎样？读工具受影响吗？
   → `_set` 失败返回 False，`pend` 拿不到可用的存储。写操作**不执行**（fail-closed）。
   读工具根本不进沙箱，完全不受影响——所以这个选择不伤可用性。
3. 为什么内存兜底只能用于单测？
   → 多副本下 A 副本挂起的动作 B 副本看不到，nginx 轮询一下确认就打空了。
   `ActionStore.source`（redis / memory）会随事件暴露，演示时一眼能看出是不是真多副本。
4. `digest` 为什么不能参与放行判断？
   → 它只是哈希，不是凭据。拿哈希当"通行证"等于把 token 降级成可枚举的值。

---

## 五、Block 4 · 图改道与确认端点（20 min）

**先读 `app/pipeline/orchestrator.py:1178-1209`**，只有 30 行，但是最容易讲错的地方：

```python
async def node_tools_done(state):
    return {}                      # 空节点，只为提供一个"汇合后"的位置

def _after_tools_done(state):
    if not state.get("pending"):
        return "generate"
    decision = state.get("decision")
    # 两层兜底：缺 decision 时按"不答"处理 —— 挂起优先于半截答案
    return "generate" if getattr(decision, "needs_rag", False) else "await_confirm"
```

**为什么要多一个空节点**：条件边如果挂在 `tools` 上，`recall → generate` 那条边**仍会触发**
`generate`——于是"等人确认"的同时，模型已经生成了一段"已为你重启"的答案。
这个 bug 只在真有人确认的场景出现，跑测试永远看不见。

**为什么是三分支而不是"有 pending 就 await_confirm"**：hybrid 的 pending 旁边还有
一条独立的检索结果。全走 `await_confirm` 会让"帮我重启 X，顺带查 OOM"永远得不到
OOM 的答案——用户问了两件事，只收到一句"请确认"。

`generate` 这一路有三处配套（`974-1021`）：

- `_pending_context`：把"本轮有动作待确认、**尚未执行**、不许说已重启/已扩容"
  写进模型上下文。拼在 `tool_context` 前面，不塞进 system prompt——
  system prompt 是全局的，这句话只对本轮成立；放进去会让后面每一轮都带着"你还有待确认动作"；
- `_pending_done`：`done` 帧自己带 `status=awaiting_confirmation` + `pending` 摘要，
  字段与 `await_confirm` 完全一致，调用方只写一份解析逻辑；
- **挂起轮不写答案缓存**：缓存的是"某个动作正在等人确认"这个瞬时状态，
  存下来下次直接命中，审批单就变成一段旧文字了。

`app/pipeline/orchestrator.py:1271-1307` 看连边：

```
dispatch ─┬─▶ tools  ─┐
          └─▶ recall ─┴─▶ tools_done ─┬─(无 pending)────▶ generate → END
                                       ├─(pending + rag)─▶ generate（done 带 status）→ END
                                       └─(pending 纯写)──▶ await_confirm → END
```

然后读 `node_tools`（`722-890`），只看三处：

- `736-738`：`with call_context(...)` —— 沙箱要记"谁、在什么场景下申请了这个动作"；
- `762-767`：**带写操作时跳过 skill**。skill 的 `previous_empty` 语义是"没查到就换路径"，
  而写操作没有"换条路径再重启一次"这种说法；
- `793`：`failed = [r for r in results if not r.ok and not r.status]`
  —— 挂起/拒绝**不计入失败**，否则"有人在审批"会把工具失败率顶上去。

再读 `1212-1268` `node_await_confirm`：不调 LLM、只做三件事（done 带 status、
带 pending 摘要、存一轮会话历史）。注意 `1218-1222` 的注释：`confirm_required` 帧
**不在这里重发**，因为 `node_tools` 已经发过——重发会让前端弹两次带 token 的框。
也注意 `1232-1236` 拼的那句"凭据已随事件流下发，N 秒内有效"——它只会出现在
**纯写挂起**这一轮。如果这句话跑到别的轮次（比如你只问了一句 OOM），
那是模型在复述历史，不是本轮真的发了凭据。**判据永远是事件流里有没有
`confirm_required` 帧**，别信正文。

最后读 `app/api/chat.py:52-63` 与 `190-263`：

- `ConfirmRequest` 只有 `{token, approve, session_id}`，**没有 tool、没有 args**；
- 端点先 `peek` 判断归属（跨会话用 token → 403，且**不删记录**，合法持有者可能还要用）；
- 执行时 `registry.call(action.tool, _confirm=req.token)` —— **连 args 都不传**。

**必须能回答**

1. 为什么执行放在 `/chat/confirm` 里直接调工具，而不是"回到图里继续跑"？
   → 重跑图会把工具再规划一次，等于让模型有机会"第二次抽出不同的参数"，
   正好破坏第 2 条边界。所以这里执行的是**那条记录**，不经过规划器、不经过 LLM。
2. 为什么不用 LangGraph 的 `interrupt()`？
   → 它需要 checkpointer 做进程内暂停 + 恢复，本项目 checkpointer 还是 backlog；
   而且多副本下"恢复"要落到同一个副本上。落库 + 新请求续跑语义等价，且天然多副本可用。
3. 为什么纯写挂起这一轮不能顺手生成一句"我将为你重启"？
   → 会变成既没有执行、又给了用户一个"已经安排上了"的错觉。终局只陈述事实。
   注意这条**只适用于纯写**：hybrid 有检索结果，那部分是可以回答的事实，所以照答。

---

## 六、测试怎么读（15 min）

`tests/test_sandbox.py` **18 个用例**，按**边界**分五组，别按文件顺序读：

| 组 | 用例（行号） | 钉住的边界 |
|---|---|---|
| 闸门本身 | `82` `93` `162` `183` `205` | 只读不过闸；写必挂起；策略先于确认；denied 文案要引用 runbook；沙箱关闭 → 拒绝（fail-closed） |
| 凭据 | `115` `134` `148` `219` `234` | 执行用快照、一次性、绑定工具、过期、审计带会话 |
| 识别与规划 | `249` `277` `407` | 写操作能规划出来；路由标 `needs_write`；**复句里的写操作不被知识信号吞掉** |
| 端到端 | `293` `365` `432` | 挂起→确认；挂起→拒绝（含"没有正文 token"）；**hybrid 既答知识又挂起** |
| 契约 | `479` | 指标与大盘字段契约（面板挂了不报错，只能靠这条） |

`tests/fake_redis.py` 的 `getdel` 是专门为沙箱补的——**没有它，一次性测试根本测不出来**。
读测试时优先看断言的消息字符串（`check("...", cond, detail)` 的第一个参数），
它们就是这套机制的验收清单。

---

## 七、自测题（12 道，分三档）

> 纯问题版（无答案）已抽到 `docs/STUDY_SANDBOX_QUESTIONS.md`，自测时用那份，答不上来再回来查。

**A 档 · 机制（能讲清就能过面试）**

1. 写请求从问题到执行，经过哪 6 个环节？各在哪个文件哪一行？
2. 闸门为什么必须在 `_make_handler`？举一个"放在上游会漏"的例子。
3. 挂起那轮 `done` 的 `status` 是什么？纯写挂起为什么不生成答案？
   hybrid 为什么又生成？两条路分别走哪个节点、`done` 帧差在哪？
4. `_confirm` 为什么从 kwargs 里 pop 掉，而不是当普通入参？

**B 档 · 边界（面试官最爱问"如果"）**

5. 拿 token 改成别的服务名，为什么执行不了？在哪一行被挡住？
6. 同一个 token 并发确认两次，会发生什么？靠什么保证？
7. 沙箱 Redis 挂了，写操作会执行吗？读操作为什么没事？
8. token 属于工具 A、却拿去确认工具 B，为什么要连记录一起删？
9. 同会话同参数为什么复用 token？不复用会出现什么现象？
10. 「重启 X 的步骤是什么」和「重启 X，并查 OOM 怎么办」判定差在哪一步？
    如果把 knowledge 的判断从分句改回整句，这两种句子分别会错成什么样？

**C 档 · 判断力（区分"用过"和"想过"）**

11. 为什么不用 `interrupt()` / 为什么不用 WebSocket 挂住等？
12. `read / write / dangerous` 三档够用吗？真实环境你会怎么分？接真实 K8s API 还缺什么？

> 12 题里能答上 9 题（且 C 档至少对 1 题），这块就算过了。
> 答不上来的，回对应 Block 重读那 30 行，不要通读。

---

## 八、高频追问 9 条（答骨架，30 秒一条）

| 问 | 答 |
|---|---|
| 审批为什么不放在前端？ | 前端只是展示层，绕过它直接调 API 就没了。闸门在服务端工具收口，**唯一入口** |
| 你怎么保证"确认的就是执行的"？ | 确认接口不收 args，执行参数只从 Redis 快照读；且执行前用快照再校验一次策略 |
| 一次性怎么做的？ | Redis `GETDEL` 原子消费 + 10 分钟 TTL；不用定时清理任务 |
| 多副本怎么办？ | 待办在 Redis 不在进程内存；实测挂起在 api-1、从 api-2 确认也能执行 |
| 用户忘了确认呢？ | TTL 到点自动消失，不需要补偿；审计里留了 `pending` 记录，能查出"批了没批" |
| 会不会审批疲劳？ | 只读操作**永不**要求确认；同会话同参数复用同一个 token，不重复弹 |
| 沙箱自己挂了会不会变成后门？ | fail-closed：没沙箱、没策略、存储写失败，一律拒绝执行 |
| 审计为什么不用 logging？ | `logging` 的 handler 有自己进程内的锁，跨进程不保证；审计用 `O_APPEND` + 单次 `os.write`，内核保证追加原子性。这条是踩过 trace 文件损坏才改的 |
| 一句话里既让重启又问知识？ | 按**分句**判：同句问流程（"重启的步骤是什么"）只读；知识在另一分句就是并列诉求，`hybrid + needs_write`。读的照答、写的挂起，`done` 自己带 `status`，且这一轮不进缓存 |

---

## 九、这套设计的真实短板（自己先说，别等人问出来）

1. **没有 checkpointer，所以"确认后的续跑"是重新执行那条记录，不是恢复图状态**。
   好处是不依赖进程；代价是"确认之后模型不能再补一句解释"。
2. **服务白名单写在代码里**（`SANDBOX_SERVICES`，`sandbox.py:120`）。真实环境该来自 CMDB，
   但写死在代码里更安全——改动必须过 review。这是权衡，不是疏忽。
3. **只有单级审批**，没有双人复核 / 变更窗口 / 配额。真要动生产，这套只够"演示级安全"。
4. **`risk` 靠声明**（`ToolSpec.risk`），漏标就是漏防护，只能靠 `sandbox_write_tools`
   gauge 与 `pending` 计数对照发现。这个失效**不报错**，是最危险的一类。
5. **参数策略是正则 + 值白名单**，能防注入形状，防不了"合法参数但业务上不该做"
   （比如高峰期扩容到 10）。那类判断需要容量系统介入。
6. **写工具是 `simulated=True`**，返回文本第一行就是 `[模拟执行 · 未真正调用编排接口]`。
   讲的时候别说成真重启——面试官问一句"你调的是哪个 API"就穿帮。
7. **意图识别是正则 + 分句，不是模型判定**。它处理不了"重启一下 X 吧，顺便说说
   OOM 怎么弄"这种口语复述（换语序、省略号、指代都够呛）。
   现在靠"漏判可恢复、误判是体验事故"的不对称取舍兜底；
   真要做稳，得让模型出候选、正则做校验，而不是反过来。

---

## 十、只有 20 分钟的话

按这个顺序，只读四处：

1. `app/tools/__init__.py:246-285`（闸门，含 `return None, dict(action.args)`）
2. `app/tools/sandbox.py:324-339`（`GETDEL` 一次性）
3. `app/pipeline/orchestrator.py:1178-1209`（三分支：汇合点为什么要前移）
4. `app/api/chat.py:52-63`（请求体里没有 args）

这四处连起来就是一套完整的答案：**在哪拦、凭什么只执行一次、为什么不能边等边答、
确认凭什么不能改参数**。

如果还有 5 分钟，补一句 `app/pipeline/intent.py:360-377`（`write_hit` / `asking_how`）：
它是"该不该拦"的源头，也是这套东西唯一一个**判错会静默失效**的地方。
