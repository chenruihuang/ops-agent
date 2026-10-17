"""把知识源组装成文档列表（纯逻辑，无 IO，可被校验器与渲染器共用）。

核心思想：**文档与评估集同源**。
评估集里的 golden_doc_ids 引用的是这里生成的 doc_id，只要组装过程
不产生悬空引用（由 scripts/check_corpus.py 强制校验），标注就不可能出错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from data.source import (
    ALERTS,
    ARCH,
    COMMANDS,
    ERROR_CODES,
    FAQ,
    LONGFORM_DOCS,
    OBSERVABILITY,
    ONCALL,
    POLICIES,
    RUNBOOKS,
    SERVICES,
)

# 文档引用模式：rb-xxx / pol-xxx / arch-xxx / alert-xxx / oncall-xxx / obs-xxx /
# cmd-xxx / faq-xx / guide-xxx / pm-xxx / spec-xxx
#
# ⚠ 同一套前缀集合出现在**三个地方**，改动必须同步，否则会出现
# "文档能生成、但引用关系抽不出来"或反之的静默不一致：
#   1. 本文件的 REF_PATTERN       —— 引用完整性校验
#   2. app/ingest/chunker.py 的 _DOC_ID_TOKEN —— 图谱边抽取
#   3. scripts/check_corpus.py 的命名规范检查 —— doc_id 格式校验
#
# 左边界不可省。原正则没有边界，导致 `svc-search-service` 里的
# `search-service` 被**子串**匹配出 `arch-service`，在引用完整性检查里
# 报出一个根本不存在的文档 id。（这是补长文档时才暴露的：新语料里
# 首次出现大量 `svc-*-service` 形态的引用。）
# 用 `(?<![a-z0-9-])` 而不是 `\b`：中文后紧跟引用（如「见 rb-xxx」）也必须能匹配，
# 而 `\b` 会把中文字符也算作 word char，从而把这种情况一起挡掉。
REF_PATTERN = re.compile(
    r"(?<![a-z0-9-])"
    r"(?:rb|pol|arch|alert|oncall|obs|cmd|faq|guide|pm|spec)"
    r"-[a-z0-9]+(?:-[a-z0-9]+)*"
)


@dataclass(frozen=True)
class Doc:
    doc_id: str
    title: str
    category: str
    tags: tuple[str, ...] = ()
    body: str = ""
    related: tuple[str, ...] = ()
    source_keys: dict = field(default_factory=dict)


# ---------------------------------------------------------------------
# 正文渲染
# ---------------------------------------------------------------------

def _md_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * len(header)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c).replace("\n", " ") for c in r) + " |")
    return "\n".join(lines)


def _related_block(related: tuple[str, ...], titles: dict[str, str]) -> str:
    if not related:
        return ""
    lines = ["", "## 关联文档"]
    for rid in related:
        lines.append(f"- `{rid}` — {titles.get(rid, '（待补充）')}")
    return "\n".join(lines)


def _render_service(svc: dict, titles: dict[str, str]) -> tuple[str, tuple[str, ...], str]:
    doc_id = f"svc-{svc['key']}"
    body: list[str] = []

    body.append("## 概述")
    body.append(
        f"{svc['name']}（服务标识 `{svc['key']}`）属于**{svc['domain']}**，"
        f"技术栈为 {svc['stack']}，是生产环境 {svc['replicas']} 副本运行的核心服务。"
    )

    body.append("")
    body.append("## 基本信息")
    body.append(_md_table(
        ["项目", "内容"],
        [
            ["服务名", svc["name"]],
            ["服务标识", svc["key"]],
            ["所属域", svc["domain"]],
            ["技术栈", svc["stack"]],
            ["端口", svc["port"]],
            ["副本数", str(svc["replicas"])],
            ["部署方式", svc["deploy"]],
            ["代码仓库", svc["repo"]],
            ["值班组", svc["oncall"]],
            ["健康检查", svc["health"]],
            ["运行时参数", svc["runtime"]],
        ],
    ))

    body.append("")
    body.append("## 依赖")
    body.append("")
    body.append("**上游（本服务依赖）**")
    body.extend(f"- {d}" for d in svc["upstreams"])
    body.append("")
    body.append("**中间件与平台**")
    body.extend(f"- {d}" for d in svc["mids"])

    body.append("")
    body.append("## 限流与配额")
    body.extend(f"- {x}" for x in svc["limits"])

    body.append("")
    body.append("## SLO")
    body.append(_md_table(
        ["指标", "目标"],
        [
            ["可用性", svc["slo_availability"]],
            ["延迟", svc["slo_latency"]],
            ["错误预算", svc["slo_budget"]],
        ],
    ))

    body.append("")
    body.append("## 常见故障与处置入口")
    body.append(_md_table(
        ["典型现象", "处置文档"],
        [[fm[0], f"`{fm[1]}`"] for fm in svc["failmodes"]],
    ))

    if svc.get("note"):
        body.append("")
        body.append("## 运维备注")
        body.append(svc["note"])

    related = tuple(dict.fromkeys(
        [fm[1] for fm in svc["failmodes"]]
        + [f"err-{svc['key']}"]
        + [f"svc-{x.split(':')[0]}" for x in svc["upstreams"] if "service" in x or "center" in x]
    ))
    tags = (svc["domain"], "服务手册", svc["key"])
    return doc_id, tags, "\n".join(body) + _related_block(related, titles), related


def _render_error(svc: dict, codes: list[tuple], titles: dict[str, str]) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    doc_id = f"err-{svc['key']}"
    body: list[str] = []
    body.append("## 说明")
    body.append(
        f"{svc['name']}（`{svc['key']}`）对外返回的错误码定义。所有错误码格式为 "
        f"`<服务前缀>-<HTTP 状态码>-<序号>`，客户端应依据「是否可重试」列决定重试策略。"
    )
    body.append("")
    body.append("## 错误码字典")
    body.append(_md_table(
        ["错误码", "含义", "触发条件", "处置建议", "是否可重试"],
        [[c[0], c[1], c[2], c[3], c[4]] for c in codes],
    ))
    body.append("")
    body.append("## 通用约定")
    body.append("- 4xx 类错误码属于调用方问题，**不可重试**；5xx 类需结合「是否可重试」列判断。")
    body.append("- 429 类错误码必须退避重试，禁止立即重试（会加剧限流）。")
    body.append("- 所有错误响应体都包含 `trace_id`，排障时优先用它关联日志与链路。")

    related = tuple(dict.fromkeys(
        [f"svc-{svc['key']}"] + _extract_refs(codes)
    ))
    tags = (svc["domain"], "错误码", svc["key"])
    return doc_id, tags, "\n".join(body) + _related_block(related, titles), related


def _extract_refs(codes: list[tuple]) -> list[str]:
    """从错误码的处置建议文字中提取形如 rb-xxx / pol-xxx 的文档引用。

    必须用正则而不是 split —— 文本里经常出现 "见 rb-cron-duplicate：终止后发实例"
    这种「引用后面直接跟中文说明」的写法，split 会把整段当成一个 id。
    """
    refs: list[str] = []
    for c in codes:
        refs.extend(REF_PATTERN.findall(str(c[3])))
    return refs


def _render_runbook(rb: dict, titles: dict[str, str]) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    doc_id = rb["key"]
    body: list[str] = []

    body.append("## 现象")
    body.append(rb["symptom"])
    body.append("")
    body.append("## 影响面")
    body.append(rb["impact"])

    body.append("")
    body.append("## 快速判定")
    body.append(_md_table(["命令 / 查询", "判读方式"], [[t[0], t[1]] for t in rb["triage"]]))

    body.append("")
    body.append("## 根因分类")
    body.extend(f"- {c}" for c in rb["causes"])

    body.append("")
    body.append("## 处置步骤")
    for s in rb["steps"]:
        body.append(f"- {s}")

    body.append("")
    body.append("## 回滚方案")
    body.append(rb["rollback"])

    related = tuple(dict.fromkeys(rb.get("related", [])))
    tags = (rb["severity"], "故障处置", rb["owner"])

    meta_block = [
        "",
        "## 处置信息",
        _md_table(
            ["项目", "内容"],
            [
                ["严重等级", rb["severity"]],
                ["责任方", rb["owner"]],
                ["MTTR 目标", rb["mttr_target"]],
                ["升级路径", rb["escalate"]],
            ],
        ),
    ]
    return doc_id, tags, "\n".join(body + meta_block) + _related_block(related, titles), related


def _render_sectioned(item: dict, titles: dict[str, str]) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    doc_id = item["key"]
    body: list[str] = []
    if item.get("summary"):
        body.append("## 摘要")
        body.append(item["summary"])
    for heading, content in item.get("sections", []):
        body.append("")
        body.append(f"## {heading}")
        body.append(content)
    related = tuple(dict.fromkeys(item.get("related", [])))
    tags = tuple(item.get("tags", [])) + ((item["severity"],) if item.get("severity") else ())
    return doc_id, tags, "\n".join(body) + _related_block(related, titles), related


def _render_longform(item: dict, titles: dict[str, str]) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    """长文档渲染：支持 dict 形式的嵌套 sections，递归渲染为 `##` / `###` / `####`。

    为什么不像 `_render_sectioned` 那样用 `(heading, content)` 元组：
    元组结构表达不了"任意深度嵌套 + 每层可选子节"，硬塞会变成
    `(h, p, [(h, p, [(h, p)])])` 这种没人愿意维护的东西。
    长文档的价值恰恰在于它带真实标题层级——切块器的层级归属逻辑
    只有在有 `###` / `####` 时才会被真正执行，所以这里的表达力是必需的。

    注意：`related` 里的引用由 `_related_block` 统一渲染成末尾的「关联文档」小节，
    该小节在切块阶段会被识别并**转成图谱边**而不进向量库（全是 id 字符串，
    向量化后是噪声）。所以这里不要在 body 里再手写一个关联文档小节。
    """
    doc_id = item["key"]
    body: list[str] = []
    if item.get("summary"):
        body.append("## 摘要")
        body.append(item["summary"])

    def emit(secs: list[dict], level: int) -> None:
        prefix = "#" * level
        for sec in secs:
            heading = str(sec.get("h") or "").strip()
            if not heading:
                continue
            body.append("")
            body.append(f"{prefix} {heading}")
            text = str(sec.get("p") or "").rstrip()
            if text:
                body.append(text)
            if sec.get("sub"):
                emit(sec["sub"], level + 1)

    emit(item.get("sections", []), 2)
    related = tuple(dict.fromkeys(item.get("related", [])))
    tags = tuple(item.get("tags", []))
    return doc_id, tags, "\n".join(body) + _related_block(related, titles), related


# ---------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------

def build_docs() -> list[Doc]:
    """返回全部文档。分两遍：先收集 id->title 映射，再渲染正文（以便填入关联文档标题）。"""
    titles: dict[str, str] = {}

    # ---- pass 1: 收集标题 ----
    for svc in SERVICES:
        titles[f"svc-{svc['key']}"] = f"{svc['name']}（{svc['key']}）服务手册"
        titles[f"err-{svc['key']}"] = f"{svc['name']}错误码字典"
    for rb in RUNBOOKS:
        titles[rb["key"]] = rb["title"]
    for group in (ALERTS, POLICIES, ARCH, ONCALL, OBSERVABILITY, COMMANDS, FAQ):
        for item in group:
            titles[item["key"]] = item["title"]
    for item in LONGFORM_DOCS:
        titles[item["key"]] = item["title"]

    # ---- pass 2: 渲染 ----
    docs: list[Doc] = []

    for svc in SERVICES:
        did, tags, body, rel = _render_service(svc, titles)
        docs.append(Doc(did, titles[did], "service", tags, body, rel,
                        {"service_key": svc["key"], "domain": svc["domain"]}))

    for svc in SERVICES:
        codes = ERROR_CODES.get(svc["key"], [])
        if not codes:
            continue
        did, tags, body, rel = _render_error(svc, codes, titles)
        docs.append(Doc(did, titles[did], "error", tags, body, rel,
                        {"service_key": svc["key"], "domain": svc["domain"]}))

    for rb in RUNBOOKS:
        did, tags, body, rel = _render_runbook(rb, titles)
        docs.append(Doc(did, titles[did], "runbook", tags, body, rel,
                        {"severity": rb["severity"], "owner": rb["owner"],
                         "mttr_target": rb["mttr_target"]}))

    group_category = [
        (ALERTS, "alert"), (POLICIES, "policy"), (ARCH, "arch"),
        (ONCALL, "oncall"), (OBSERVABILITY, "obs"), (COMMANDS, "cmd"), (FAQ, "faq"),
    ]
    for group, category in group_category:
        for item in group:
            did, tags, body, rel = _render_sectioned(item, titles)
            docs.append(Doc(did, titles[did], category, tags, body, rel, {}))

    # 长文档：category 由知识源自带（guide / postmortem / spec）。
    # 它们按**文档形态**分类而不是按主题——这正是新增它们的目的：
    # 给切块器提供"深层标题 + 超长小节 + 长表格"这些原文语料里没有的形态。
    for item in LONGFORM_DOCS:
        did, tags, body, rel = _render_longform(item, titles)
        docs.append(Doc(
            did,
            titles[did],
            str(item.get("category") or "guide"),
            tags,
            body,
            rel,
            {"longform": True},
        ))

    # 校验 id 唯一
    seen: set[str] = set()
    for d in docs:
        if d.doc_id in seen:
            raise ValueError(f"doc_id 重复：{d.doc_id}")
        seen.add(d.doc_id)

    return docs


def doc_index() -> dict[str, Doc]:
    return {d.doc_id: d for d in build_docs()}


def all_related_refs() -> dict[str, list[str]]:
    """返回 {doc_id: 引用的 doc_id 列表}，供校验器使用。"""
    return {d.doc_id: list(d.related) for d in build_docs()}


if __name__ == "__main__":  # pragma: no cover
    ds = build_docs()
    print(f"文档总数：{len(ds)}")
    from collections import Counter
    for cat, n in sorted(Counter(d.category for d in ds).items()):
        print(f"  {cat:10s} {n}")
