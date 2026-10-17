"""意图路由评估 —— 用 52 条标注跑真实准确率，而不是"我觉得规则没错"。

为什么这个脚本必须存在
--------------------
召回率有 `eval_retrieval.py`，缓存有单测，唯独路由如果只靠人工试几个例子，
那就是整个链路里**唯一没有数字支撑的一环**，而它恰好是决定"答案从哪来"的环节。

跑法
----
    python -m scripts.check_intent                       # 全量
    python -m scripts.check_intent --min-accuracy 0.9    # 低于阈值就退出码非 0（可挂 CI）
    python -m scripts.check_intent -v                    # 打印全部误判

两个口径都要看
--------------
标注集里 rag 有 45 条、tool/hybrid/chitchat 共 7 条。
**全判 rag 就能拿到 86.5% 的总准确率** —— 所以只看总准确率等于自我欺骗。
脚本因此额外报"非 rag 题召回"：那 7 条路由题里判对了几个。这才是路由能力的真实读数。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.intent import (  # noqa: E402
    INTENTS,
    evaluate_routing,
    format_routing_report,
    intent_stats,
)

NON_RAG = ("tool", "hybrid", "chitchat")


def main() -> int:
    parser = argparse.ArgumentParser(description="意图路由评估")
    parser.add_argument("--questions", default="eval/questions.json")
    parser.add_argument("--min-accuracy", type=float, default=0.0,
                        help="总准确率下限，低于则退出码 1")
    parser.add_argument("--min-nonrag-recall", type=float, default=0.0,
                        help="非 rag 题召回下限（路由能力的真实读数），低于则退出码 1")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印全部误判样例")
    args = parser.parse_args()

    qs = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    print("=" * 60)
    print("意图路由评估")
    print("=" * 60)
    print(f"标注集：{args.questions}")
    print(f"意图分布：{intent_stats(qs)}")
    print(f"  ⚠️ 分布极不平衡：全判 rag 的准确率就有 "
          f"{intent_stats(qs).get('rag', 0) / max(1, len(qs)):.4f}，"
          f"所以必须同时看下面的非 rag 口径")
    print()

    overall = evaluate_routing(qs)
    print(format_routing_report(overall))
    print()

    nonrag = evaluate_routing(qs, include=NON_RAG)
    print("-" * 60)
    print(f"非 rag 题（tool/hybrid/chitchat）召回：{nonrag.correct}/{nonrag.total} "
          f"= {nonrag.accuracy:.4f}")
    if nonrag.errors and not args.verbose:
        print("  误判样例（用 -v 看全部）：")
        for qid, question, exp, act in nonrag.errors[:5]:
            print(f"    q-{qid:<3} 期望 {exp:<8} 实际 {act:<8} {question[:44]}")
    print()

    failed = []
    if overall.accuracy < args.min_accuracy:
        failed.append(f"总准确率 {overall.accuracy:.4f} < {args.min_accuracy}")
    if nonrag.accuracy < args.min_nonrag_recall:
        failed.append(f"非 rag 召回 {nonrag.accuracy:.4f} < {args.min_nonrag_recall}")
    if failed:
        print("!! 未达阈值：")
        for f in failed:
            print(f"   - {f}")
        return 1

    print(f"✅ 通过（总准确率 {overall.accuracy:.4f}，非 rag 召回 {nonrag.accuracy:.4f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
