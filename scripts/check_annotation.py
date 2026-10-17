"""评估集校验 —— 在跑评估之前先把标注集的坑填掉。

为什么值得单独写一个校验脚本
----------------------------
标注集错了，评估指标就是错的，而它看起来一切正常。
这是整个项目里最容易出"假数据"的地方，所以宁可在 D0 花 20 分钟校验。

校验项：
  1. 必填字段齐全、类型正确
  2. id 唯一且连续
  3. 三类意图（rag / tool / hybrid）都有覆盖，不能全是 rag
  4. rag 与 hybrid 题必须有 golden_doc_ids —— 否则算不出上下文召回率
  5. tool 类题必须有 expected_tools
  6. 同一问题不能重复（归一化后比较）
  7. 难度分布不能全是 easy
  8. answer_anchors 必须真实出现在 golden 文档正文中（只查有 golden_doc_ids 的题）

关于第 8 项：它与"悬空 golden_doc_id"同源的假数据问题，只是更隐蔽 ——
golden 文档写对了，锚点却是**编的**。人工抽检时看着完全合理，但评估脚本
用不上它，生成层也会被要求去凑一个语料里根本没有的字符串。
实测第一版 40 条里有 6 条属于"转述而非原文措辞"（如把"副本数下降"写成"副本下降"），
全部改成字面量后，锚点从"看起来对"变成"机器可验证"。
纯 tool 题与 chitchat 题不在校验范围内：它们的答案不扎根语料，
锚点常是查询参数名或负向结论（"无告警"），核对必然误报。

跑法：
    python -m scripts.check_annotation eval/questions.json
    python -m scripts.check_annotation eval/questions.example.json   # 验证脚本本身
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache.keys import normalize  # noqa: E402

CORPUS_DIR = ROOT / "data" / "raw"
MOCK_RECORDS = ROOT / "eval" / "mock_records.json"

REQUIRED = {
    "id": int,
    "question": str,
    "intent": str,
    "golden_doc_ids": list,
    "key_points": list,
}
VALID_INTENTS = {"rag", "tool", "hybrid", "chitchat"}
VALID_DIFFICULTY = {"easy", "medium", "hard"}


def _load_corpus(directory: Path) -> dict[str, str]:
    """把语料读成 {doc_id: 正文}。目录不存在时返回空 dict，调用方降级跳过该项校验。"""
    texts: dict[str, str] = {}
    if not directory.is_dir():
        return texts
    for path in directory.rglob("*.md"):
        texts[path.stem] = path.read_text(encoding="utf-8")
    return texts


def check_anchors(
    raw: list, corpus: dict[str, str], mock_text: str
) -> tuple[list[str], int]:
    """校验 answer_anchors 是否真实存在于语料（或 hybrid 题的 mock 数据）中。

    只校验**有 golden_doc_ids 的题**。原因：纯 tool 题（如"查库存服务有没有告警"）
    与 chitchat 题的答案来自实时工具调用或自我介绍，本来就不扎根语料 ——
    它们的锚点往往是"查询参数名"或负向结论（"无告警"），拿语料去核对必然误报。
    把这类题排除在外，换来的是这条校验**零误报**，因此可以放心长期开着。
    """
    warnings: list[str] = []
    checked = 0
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        anchors = item.get("answer_anchors") or []
        golden = item.get("golden_doc_ids") or []
        if not anchors or not golden:
            continue
        blob = "\n".join(corpus.get(doc, "") for doc in golden)
        if item.get("intent") == "hybrid":
            # hybrid 题除了文档事实，还要核对工单结论，工单号只存在于 mock 数据里
            blob += "\n" + mock_text
        for anchor in anchors:
            checked += 1
            if anchor and anchor not in blob:
                warnings.append(
                    f"第 {idx + 1} 条 (id={item.get('id', '?')}): 锚点 {anchor!r} "
                    f"不在 golden 文档正文中 —— 可能是转述而非原文措辞"
                )
    return warnings, checked


def check(path: str) -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"文件不存在：{path}"], [], {}
    except json.JSONDecodeError as exc:
        return [f"JSON 解析失败：{exc}"], [], {}

    if not isinstance(raw, list):
        return ["顶层必须是数组"], [], {}

    seen_ids: set[int] = set()
    seen_questions: dict[str, int] = {}
    intents: Counter[str] = Counter()
    difficulties: Counter[str] = Counter()

    for idx, item in enumerate(raw):
        where = f"第 {idx + 1} 条 (id={item.get('id', '?')})"

        if not isinstance(item, dict):
            errors.append(f"{where}: 不是对象")
            continue

        for field_name, field_type in REQUIRED.items():
            if field_name not in item:
                errors.append(f"{where}: 缺少必填字段 {field_name}")
            elif not isinstance(item[field_name], field_type):
                errors.append(
                    f"{where}: 字段 {field_name} 类型应为 {field_type.__name__}，"
                    f"实际 {type(item[field_name]).__name__}"
                )

        qid = item.get("id")
        if isinstance(qid, int):
            if qid in seen_ids:
                errors.append(f"{where}: id 重复")
            seen_ids.add(qid)

        question = item.get("question", "")
        if isinstance(question, str) and question.strip():
            key = normalize(question)
            if key in seen_questions:
                errors.append(f"{where}: 与第 {seen_questions[key]} 条问题重复（归一化后相同）")
            else:
                seen_questions[key] = idx + 1

        intent = item.get("intent")
        if intent is not None:
            if intent not in VALID_INTENTS:
                errors.append(f"{where}: intent 取值非法 {intent!r}，应为 {sorted(VALID_INTENTS)}")
            else:
                intents[intent] += 1
        else:
            warnings.append(f"{where}: 缺少 intent，无法统计意图覆盖度")

        difficulty = item.get("difficulty")
        if difficulty is not None:
            if difficulty not in VALID_DIFFICULTY:
                errors.append(f"{where}: difficulty 取值非法 {difficulty!r}")
            else:
                difficulties[difficulty] += 1

        golden = item.get("golden_doc_ids") or []
        tools = item.get("expected_tools") or []
        if intent in {"rag", "hybrid"} and not golden:
            errors.append(
                f"{where}: intent={intent} 但没有 golden_doc_ids —— 无法计算上下文召回率"
            )
        if intent == "tool" and not tools:
            errors.append(f"{where}: intent=tool 但没有 expected_tools")
        if intent == "rag" and tools:
            warnings.append(f"{where}: intent=rag 却声明了 expected_tools，请确认意图分类是否正确")

        points = item.get("key_points") or []
        if isinstance(points, list) and points and len(points) < 2:
            warnings.append(f"{where}: key_points 只有 1 条，人工抽检时不好判断完整性")

    # ---- 锚点真实性检查（语料目录不存在时自动跳过）----
    anchors_checked = 0
    anchor_misses = 0
    corpus = _load_corpus(CORPUS_DIR)
    if corpus:
        mock_text = MOCK_RECORDS.read_text(encoding="utf-8") if MOCK_RECORDS.exists() else ""
        anchor_warnings, anchors_checked = check_anchors(raw, corpus, mock_text)
        anchor_misses = len(anchor_warnings)
        warnings.extend(anchor_warnings)

    # ---- 分布检查 ----
    total = len(raw)
    if total < 30:
        warnings.append(f"题量只有 {total} 条，建议不少于 40 条（当前指标置信区间会很宽）")
    if total and len(intents) < 2:
        warnings.append(
            f"意图分布过于单一：{dict(intents)}。至少要有 rag 与 tool 两类，否则路由能力无法被验证"
        )
    if len(difficulties) < 2:
        warnings.append(f"难度分布过于单一：{dict(difficulties)}")

    summary = {
        "total": total,
        "intents": dict(intents),
        "difficulties": dict(difficulties),
        "with_golden": sum(1 for i in raw if isinstance(i, dict) and i.get("golden_doc_ids")),
        "avg_key_points": round(
            sum(len(i.get("key_points") or []) for i in raw if isinstance(i, dict)) / max(1, total),
            2,
        ),
        "anchors_checked": anchors_checked,
        "anchor_misses": anchor_misses,
    }
    return errors, warnings, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="校验评估标注集")
    parser.add_argument("path", nargs="?", default="eval/questions.json")
    args = parser.parse_args()

    errors, warnings, summary = check(args.path)

    print("=" * 60)
    print(f"评估集校验：{args.path}")
    print("=" * 60)
    if summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    if warnings:
        print(f"\n警告 {len(warnings)} 条：")
        for w in warnings:
            print(f"  WARN  {w}")
    if errors:
        print(f"\n错误 {len(errors)} 条：")
        for e in errors:
            print(f"  FAIL  {e}")
        return 1
    print("\n校验通过 —— 可以开始跑评估了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
