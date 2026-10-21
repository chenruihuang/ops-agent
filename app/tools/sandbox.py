"""安全沙箱：把「写操作」从「只读查询」里切出来，必须人工确认才能执行。

为什么需要它
------------
本项目的工具分两类：只读（查工单/资产/告警）和写（重启服务、调副本数）。
只读工具错了顶多答非所问；写工具错了是**生产事故**。
而调用方是 LLM —— 会幻觉、会被文档里的字样带偏、会被用户一句话说动。
把模型的输出直接变成运维命令的输入，是这套系统唯一不能接受的设计。

四条边界（缺任何一条都能被绕过）
--------------------------------
1. **策略先于执行**：参数校验在工具内部、在任何 IO 之前。形状不对
   （服务名带分号、副本数 100）直接 deny，连确认机会都不给 ——
   「让人确认一条非法命令」只是把风险转给人。
2. **确认绑定参数**：token 里存的是**已经渲染好的参数快照**。确认执行时，
   工具参数**只从这份快照读**，不从请求体读。
   这条是整套设计的关键：如果确认接口接受 `{token, args}`，
   那「确认过 A 却执行了 B」就是必然，而不是可能。
3. **一次性 + 过期**：Redis `GETDEL` 原子消费。同一个 token 只能成功一次
   （重放拿不到记录），TTL 到点自动消失，不需要定时清理任务。
4. **留痕**：审计日志一行一个 JSON（谁、什么工具、什么参数、批没批、结果）。

为什么默认拒绝（fail-closed）
----------------------------
沙箱不可用（Redis 挂了、策略表里没有这个工具）时，写操作**不执行**。
反过来（fail-open）会让「Redis 挂掉」变成「绕过审批的后门」——
而 Redis 挂恰恰是最容易发生的故障。只读工具完全不受影响，
所以这个选择不伤可用性，它只把风险从"静默放行"换成"明确拒绝"。

为什么挂起后落库，而不是把连接挂住等人点
----------------------------------------
挂 10 分钟等一个 click，中间任何一层代理都会掐断（nginx 默认 60s），
进程重启还会全丢。所以本轮**正常结束**（done + awaiting_confirmation），
待办落 Redis，人来确认时发一个新请求续跑。
代价是一次交互要两次请求，换来的是它在多副本 / 重启 / 断连下都不丢。

与 LangGraph interrupt 的关系
----------------------------
图本身支持 `interrupt()`（进程内暂停 + checkpointer 恢复），但本项目
checkpointer 还是 backlog（见 README）。所以这里用「落库 + 新请求续跑」
实现同一个语义，且不依赖 checkpointer —— 多副本下天然可用。
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.observability.metrics import SANDBOX_CONFIRM_SECONDS, SANDBOX_DECISIONS

log = logging.getLogger(__name__)

# =====================================================================
# 风险分级
# =====================================================================
RISK_READ = "read"
RISK_WRITE = "write"
RISK_DANGER = "dangerous"
RISK_LEVELS = (RISK_READ, RISK_WRITE, RISK_DANGER)

# 需要人工确认的等级。
#
# 只读**永不确认**：否则每个"查一下工单"都要点一次同意，两天之后人就会
# 闭眼点"允许"—— 审批疲劳是这类机制最常见的失效方式，而且失效时没有任何信号。
# 写与危险分开只为在事件里显示严重程度，闸门行为一致（都要确认）。
CONFIRM_RISKS = (RISK_WRITE, RISK_DANGER)


# =====================================================================
# 参数策略（白名单，不是黑名单）
# =====================================================================
# 为什么是白名单：黑名单永远漏。参数会流向 shell / API / SQL，
# 想穷举"危险的字符"必然失败；而写工具的参数本来就少，
# 枚举"合法的形状"是可行的 —— 不可行就说明这个工具不该被自动调用。
@dataclass(frozen=True, slots=True)
class ArgRule:
    """一个参数的合法性规则。

    `choices` 非空时优先按**值白名单**判 —— 比正则强得多：
    正则只能管"长得像不像服务名"，白名单能管"这个服务存不存在"。
    `pattern` 用 fullmatch（不是 search）：漏掉一个 `^…$` 就等于允许
    "order-service; rm -rf /" 通过。
    """

    pattern: str = r".+"
    desc: str = ""
    choices: tuple[str, ...] = ()

    def check(self, value: Any) -> str | None:
        """返回错误原因；None 表示通过。"""
        text = str(value)
        if self.choices and text not in self.choices:
            return f"{self.desc or '参数'} 不在允许清单内：{text}"
        if not re.fullmatch(self.pattern, text):
            return f"{self.desc or '参数'} 格式不合法：{text!r}"
        return None


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    risk: str
    rules: Mapping[str, ArgRule]
    note: str = ""


# 允许被自动操作的服务清单。
#
# 取的是 mock 台账里真实存在的服务名。真实环境里这份清单应当来自 CMDB，
# 而不是写在代码里 —— 但在代码里反而更安全：改动它必须过一次 code review。
SANDBOX_SERVICES: tuple[str, ...] = (
    "gateway",
    "inventory-service",
    "notify-service",
    "order-service",
    "payment-gateway",
    "search-service",
)

# 绝不能自动执行的动作：动作 → 服务 → 依据。
#
# `payment-gateway` 禁止重启不是拍脑袋，是知识库里那条复盘写的
# 「禁止重启支付服务（打断资金流水）」—— 策略里引用了它，评审时能对上账。
FORBIDDEN_ACTIONS: Mapping[str, Mapping[str, str]] = {
    "restart_service": {
        "payment-gateway": "知识库 rb-payment-flow 明确禁止：重启会打断资金流水",
    },
    "scale_service": {
        "gateway": "网关副本数由容量规划单独管理，不走自助扩容",
    },
}

POLICIES: Mapping[str, ToolPolicy] = {
    "restart_service": ToolPolicy(
        risk=RISK_DANGER,
        rules={
            "service": ArgRule(r"[a-z][a-z0-9-]{2,40}", "服务名", SANDBOX_SERVICES),
            "reason": ArgRule(r"[^;|&$`<>\\\n]{2,120}", "变更原因"),
        },
        note="重启会中断正在处理的请求，属于危险操作",
    ),
    "scale_service": ToolPolicy(
        risk=RISK_WRITE,
        rules={
            "service": ArgRule(r"[a-z][a-z0-9-]{2,40}", "服务名", SANDBOX_SERVICES),
            # 副本数写死 1..10：上限是防止"手滑多打一个 0"把集群打爆
            "replicas": ArgRule(r"(?:[1-9]|10)", "副本数（1~10）"),
        },
        note="调整副本数会改变容量与成本",
    ),
}


def policy_of(tool: str) -> ToolPolicy | None:
    return POLICIES.get(tool)


def validate_call(tool: str, args: Mapping[str, Any]) -> tuple[bool, str]:
    """执行前的参数校验。**参数白名单**：没声明的键一律拒绝。

    返回 (ok, reason)。ok=False 时调用方应当直接 deny，不进确认流程。
    """
    pol = POLICIES.get(tool)
    if pol is None:
        # 没策略 = 不知道怎么判断它安全 —— 那它就是不安全
        return False, f"工具 {tool} 未声明参数策略，拒绝执行"

    extra = sorted(set(args) - set(pol.rules))
    if extra:
        return False, f"参数不在白名单内：{extra}"

    for key, rule in pol.rules.items():
        if key not in args or args[key] in (None, ""):
            continue
        err = rule.check(args[key])
        if err is not None:
            return False, err

    forbidden = FORBIDDEN_ACTIONS.get(tool, {})
    target = str(args.get("service") or "")
    if target in forbidden:
        return False, forbidden[target]

    return True, ""


# =====================================================================
# 待确认动作
# =====================================================================
@dataclass(slots=True)
class PendingAction:
    """一条挂起待确认的动作。

    `args` 是**已经渲染好的参数快照**（skill/planner 填过模板的结果），
    确认执行时只认这份快照 —— 见模块 docstring 第 2 条边界。
    """

    token: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    risk: str = RISK_WRITE
    reason: str = ""
    session_id: str = ""
    message: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    simulated: bool = False

    @property
    def digest(self) -> str:
        """工具名 + 参数的稳定指纹。用于去重与审计对账（不用于放行判断）。"""
        payload = json.dumps(
            {"tool": self.tool, "args": self.args}, ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_event(self) -> dict[str, Any]:
        """SSE `confirm_required` 帧。

        参数原样给出：要批的是"重启 order-service，原因：发布后连接泄漏"，
        只给一个 token 等于让人盲签。
        """
        return {
            "type": "confirm_required",
            "token": self.token,
            "tool": self.tool,
            "args": dict(self.args),
            "risk": self.risk,
            "reason": self.reason,
            "simulated": self.simulated,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "confirm_hint": 'POST /chat/confirm {"token": "...", "approve": true}',
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token, "tool": self.tool, "args": dict(self.args),
            "risk": self.risk, "reason": self.reason, "session_id": self.session_id,
            "message": self.message, "created_at": self.created_at,
            "expires_at": self.expires_at, "simulated": self.simulated,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PendingAction":
        return cls(
            token=str(raw.get("token") or ""),
            tool=str(raw.get("tool") or ""),
            args=dict(raw.get("args") or {}),
            risk=str(raw.get("risk") or RISK_WRITE),
            reason=str(raw.get("reason") or ""),
            session_id=str(raw.get("session_id") or ""),
            message=str(raw.get("message") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
            simulated=bool(raw.get("simulated")),
        )


class ActionStore:
    """待确认动作的存储。Redis 优先，内存兜底。

    **内存兜底只用于离线单测**：多副本下 A 副本挂起的动作 B 副本看不到，
    nginx 轮询一下确认请求就打空了。所以 `source` 会随事件一起暴露，
    演示时能一眼看出"现在是不是真的在多副本下跑"。
    """

    def __init__(
        self,
        client: Any = None,
        *,
        ttl: int = 600,
        prefix: str = "ops:v1:sandbox",
    ) -> None:
        self._client = client
        self.ttl = ttl
        self.prefix = prefix
        self._mem: dict[str, tuple[dict[str, Any], float]] = {}
        self.source = "redis" if client is not None else "memory"

    # ---------------- 键 ----------------
    def _key(self, token: str) -> str:
        return f"{self.prefix}:pending:{token}"

    def _dedupe_key(self, session_id: str, digest: str) -> str:
        return f"{self.prefix}:digest:{session_id or '-'}:{digest}"

    # ---------------- 写 ----------------
    async def put(self, action: PendingAction) -> PendingAction:
        """落一条待办。同一会话内**同工具同参数**复用同一个 token ——
        否则模型把同一个动作规划两遍，用户就要点两次一模一样的确权。"""
        dk = self._dedupe_key(action.session_id, action.digest)
        existing = await self._get(dk)
        if existing:
            try:
                return PendingAction.from_dict(json.loads(existing))
            except (TypeError, ValueError):
                pass

        payload = json.dumps(action.as_dict(), ensure_ascii=False)
        await self._set(self._key(action.token), payload)
        await self._set(dk, payload)
        return action

    # ---------------- 读/消费 ----------------
    async def peek(self, token: str) -> PendingAction | None:
        raw = await self._get(self._key(token))
        if raw is None:
            return None
        try:
            return PendingAction.from_dict(json.loads(raw))
        except (TypeError, ValueError):
            return None

    async def consume(self, token: str) -> PendingAction | None:
        """**原子**取出并删除。

        非原子（先 GET 再 DEL）在并发下会双放行：两个请求同时 GET 到记录、
        都判"有效"、都去执行 —— 于是同一句"重启"被执行了两次。
        """
        raw = await self._take(self._key(token))
        if raw is None:
            return None
        try:
            action = PendingAction.from_dict(json.loads(raw))
        except (TypeError, ValueError):
            return None
        # 顺手清掉去重键，避免同参数动作确认后又被复用（"确认过的"不该再次挂起）
        await self._del(self._dedupe_key(action.session_id, action.digest))
        return action

    async def discard(self, token: str) -> bool:
        raw = await self._take(self._key(token))
        return raw is not None

    # ---------------- 底层 ----------------
    async def _get(self, key: str) -> str | None:
        if self._client is not None:
            try:
                raw = await self._client.get(key)
                return raw.decode("utf-8") if isinstance(raw, bytes) else raw
            except Exception as exc:  # noqa: BLE001 - 存储故障不能让工具层 500
                log.warning("沙箱存储读取失败（%s）：%s", key, exc)
                return None
        item = self._mem.get(key)
        if item is None:
            return None
        payload, expires_at = item
        if expires_at and expires_at < time.time():
            self._mem.pop(key, None)
            return None
        return payload

    async def _set(self, key: str, payload: str) -> bool:
        if self._client is not None:
            try:
                await self._client.set(key, payload, ex=self.ttl)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("沙箱存储写入失败（%s）：%s", key, exc)
                return False
        self._mem[key] = (payload, time.time() + self.ttl)
        return True

    async def _take(self, key: str) -> str | None:
        if self._client is not None:
            try:
                getdel = getattr(self._client, "getdel", None)
                if getdel is not None:
                    raw = await getdel(key)
                else:  # pragma: no cover - 老版本 redis-py 才走这里
                    log.warning("redis 客户端无 getdel，退化为 GET+DEL（非原子）")
                    raw = await self._client.get(key)
                    if raw is not None:
                        await self._client.delete(key)
                return raw.decode("utf-8") if isinstance(raw, bytes) else raw
            except Exception as exc:  # noqa: BLE001
                log.warning("沙箱存储消费失败（%s）：%s", key, exc)
                return None
        item = self._mem.pop(key, None)
        if item is None:
            return None
        payload, expires_at = item
        return None if (expires_at and expires_at < time.time()) else payload

    async def _del(self, key: str) -> None:
        if self._client is not None:
            try:
                await self._client.delete(key)
            except Exception:  # noqa: BLE001
                pass
            return
        self._mem.pop(key, None)


# =====================================================================
# 审计
# =====================================================================
class AuditLog:
    """写/危险动作的审计日志，一行一个 JSON。

    **为什么用 os.write(O_APPEND) 而不是 open("a")**：
    本项目已经踩过一次 —— `data/traces/2026-10-19.jsonl` 里出现"行首半条 +
    行尾完整记录"的损坏，原因是两个 api 副本同时用 Python 缓冲写同一个文件。
    审计日志比 trace 更不能坏（它是出事之后唯一的凭据），所以：
      - `O_APPEND` + **单次** `os.write`，内核保证追加原子性（不受进程内缓冲影响）；
      - 不用 `logging`（它的 handler 有自己的锁，跨进程仍然不保证）。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._warned = False
        # 写失败必须能被**查询**，而不只是打一行日志：
        # `/admin/sandbox/audit` 返回空列表时，"没有动作"和"写不进去"
        # 在屏幕上长得一模一样 —— 这正是本项目最忌讳的那类静默失败
        # （第一次跑就踩到了：`./data` 是只读挂载，审计一条没落下来）。
        self.last_error = ""

    @property
    def writable(self) -> bool:
        return not self.last_error

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            self.last_error = ""
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            # 审计写失败**不能**阻断业务，但必须只喊一次（否则日志会被刷爆）
            if not self._warned:
                self._warned = True
                log.warning("审计日志写入失败（%s）：%s —— 后续不再提示", self.path, exc)

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近 N 条，供演示/排障直接看。读失败返回空列表。"""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


# =====================================================================
# 调用上下文（会话 / 原始问题）
# =====================================================================
# 为什么用 ContextVar 而不是把 session_id 塞进工具参数：
# 工具参数是**给 LLM 看的**。多一个 `_session` 参数，模型就有机会编造它、
# 或者被用户套出别的会话 id —— 而它应该是调用方的上下文，不是可传参。
_CTX: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "sandbox_call_context", default={}
)


@contextlib.contextmanager
def call_context(*, session_id: str = "", message: str = "") -> Iterator[None]:
    """在工具调用期间设置会话上下文。`asyncio.gather` 会把当前 context
    复制进子 task，所以并发调用也能各自读到正确的值。"""
    token = _CTX.set({"session_id": session_id or "", "message": message or ""})
    try:
        yield
    finally:
        _CTX.reset(token)


def current_context() -> dict[str, str]:
    return _CTX.get()


# =====================================================================
# 沙箱
# =====================================================================
@dataclass(frozen=True, slots=True)
class Decision:
    """策略判定结果。`action` 只有三种：allow / confirm / deny。"""

    action: str
    risk: str = RISK_READ
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class Sandbox:
    def __init__(
        self,
        store: ActionStore | None = None,
        *,
        audit: AuditLog | None = None,
        enabled: bool = True,
        ttl: int = 600,
    ) -> None:
        self.store = store if store is not None else ActionStore(ttl=ttl)
        self.audit = audit
        self.enabled = enabled
        self.ttl = ttl

    # ---------------- 判定 ----------------
    def precheck(self, tool: str, risk: str, args: Mapping[str, Any]) -> Decision:
        """执行前的策略判定。纯计算，不碰存储（可以在单测里穷举）。"""
        if risk == RISK_READ:
            return Decision("allow", RISK_READ, "只读工具")
        if not self.enabled:
            # 关掉沙箱 ≠ 放行（见模块 docstring：fail-closed）
            return Decision("deny", risk, "沙箱已关闭，写操作一律拒绝")
        ok, reason = validate_call(tool, args)
        if not ok:
            return Decision("deny", risk, reason)
        return Decision("confirm", risk, POLICIES[tool].note or "该操作会改变线上状态")

    async def pend(
        self,
        tool: str,
        risk: str,
        args: Mapping[str, Any],
        *,
        reason: str = "",
        simulated: bool = False,
    ) -> PendingAction:
        """挂起一条待确认动作，返回带 token 的记录。"""
        ctx = current_context()
        now = time.time()
        action = PendingAction(
            token=secrets.token_urlsafe(18),
            tool=tool,
            args=dict(args),
            risk=risk,
            reason=reason,
            session_id=ctx.get("session_id", ""),
            message=ctx.get("message", "")[:200],
            created_at=now,
            expires_at=now + self.ttl,
            simulated=simulated,
        )
        stored = await self.store.put(action)
        self._audit("pending", stored)
        SANDBOX_DECISIONS.labels(decision="pending", risk=risk).inc()
        return stored

    async def grant(self, token: str, tool: str) -> tuple[PendingAction | None, str]:
        """消费 token。返回值第一项为 None 时，第二项是原因（给用户看）。

        工具名必须对得上：token 是**动作级**凭证，不是"通行证"。
        对不上也要删掉记录 —— 否则它就成了一个可以反复试探的预言机。
        """
        if not token:
            return None, "缺少确认凭据"
        action = await self.store.consume(token)
        if action is None:
            SANDBOX_DECISIONS.labels(decision="invalid_grant", risk="unknown").inc()
            return None, "确认凭据无效、已用过或已过期"
        if action.tool != tool:
            self._audit("invalid_grant", action, note=f"token 属于 {action.tool}，被用于 {tool}")
            SANDBOX_DECISIONS.labels(decision="invalid_grant", risk=action.risk).inc()
            return None, f"确认凭据与动作不匹配（凭据属于 {action.tool}）"
        return action, ""

    async def reject(self, token: str) -> PendingAction | None:
        action = await self.store.consume(token)
        if action is None:
            return None
        self._audit("rejected", action)
        SANDBOX_DECISIONS.labels(decision="rejected", risk=action.risk).inc()
        self._observe_confirm("rejected", action)
        return action

    def approved(self, action: PendingAction) -> None:
        self._audit("approved", action)
        SANDBOX_DECISIONS.labels(decision="approved", risk=action.risk).inc()
        self._observe_confirm("approved", action)

    def denied(self, tool: str, risk: str, args: Mapping[str, Any], reason: str) -> None:
        # 被策略拒绝时**还没有** token（连挂起都没发生），但字段要齐 ——
        # 审计的消费方（脚本、人眼）不该为"某些行少一个键"写分支。
        self.audit and self.audit.write(
            "denied", token="", tool=tool, risk=risk, args=dict(args), reason=reason,
            session_id=current_context().get("session_id", ""),
            message=current_context().get("message", "")[:200],
        )
        SANDBOX_DECISIONS.labels(decision="denied", risk=risk).inc()

    # ---------------- 内部 ----------------
    def _audit(self, event: str, action: PendingAction, **extra: Any) -> None:
        if self.audit is None:
            return
        self.audit.write(
            event,
            token=action.token[:8],
            tool=action.tool,
            args=action.args,
            risk=action.risk,
            session_id=action.session_id,
            message=action.message,
            digest=action.digest,
            simulated=action.simulated,
            **extra,
        )

    def _observe_confirm(self, decision: str, action: PendingAction) -> None:
        waited = max(0.0, time.time() - (action.created_at or time.time()))
        SANDBOX_CONFIRM_SECONDS.labels(decision=decision).observe(waited)


# =====================================================================
# 装配
# =====================================================================
def build_sandbox(
    *,
    settings: Any = None,
    store: ActionStore | None = None,
    client: Any = None,
) -> Sandbox:
    """按配置装配沙箱。client 是 redis 异步客户端（可为 None → 内存兜底）。"""
    enabled = bool(getattr(settings, "sandbox_enabled", True)) if settings else True
    ttl = int(getattr(settings, "sandbox_pending_ttl", 600)) if settings else 600
    path = getattr(settings, "sandbox_audit_path", "") if settings else ""
    audit = AuditLog(path or "data/audit/sandbox.jsonl")
    return Sandbox(
        store if store is not None else ActionStore(client, ttl=ttl),
        audit=audit,
        enabled=enabled,
        ttl=ttl,
    )
