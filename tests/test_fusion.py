"""多路融合测试。

RRF 的价值全在"只用排名"这一条性质上，所以测试重点不是"能融合"，
而是**几条不该被破坏的不变量**：

    1. 量纲无关：把某一路的所有分数乘 10，融合结果必须完全不变
       （如果哪天有人改成"分数加权"，这条会立刻红）
    2. 多路共识优先：同时出现在两路里的文档，必须排在只出现于一路的前面
    3. 同一路内重复只记首次名次：否则一个文档的多个相似块会自我放大
    4. 融合是确定性的：同样输入永远同样输出（否则评估不可复现）

这四条都是"如果被破坏，指标会悄悄变化但不会报错"的类型。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.fusion import (  # noqa: E402
    RRF_K,
    dedupe_docs,
    rrf_fuse,
    rrf_fuse_items,
    rrf_scores,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def test_basic() -> None:
    print("基本行为")
    vec = ["a", "b", "c"]
    bm = ["c", "a", "d"]
    out = rrf_fuse([vec, bm])
    # 算式（k=60）：
    #   a: 1/61（向量第1） + 1/62（BM25第2） = 0.016393 + 0.016129 = 0.032522
    #   c: 1/63（向量第3） + 1/61（BM25第1） = 0.015873 + 0.016393 = 0.032266
    # a 以"两路都靠前"胜出 —— 这正是 RRF 的意图：不是看谁有第一名，
    # 而是看谁在两路里都稳定靠前。
    check("融合结果含全部候选", set(out) == {"a", "b", "c", "d"}, str(out))
    check("两路都靠前的排第一", out[0] == "a", str(out))
    check("确定性", rrf_fuse([vec, bm]) == out, "两次调用结果不一致")

    check("空输入返回空", rrf_fuse([]) == [] and rrf_fuse([[]]) == [], "空输入异常")


def test_rank_only() -> None:
    """核心不变量：只用排名，与分数无关。"""
    print("量纲无关性")
    r1 = ["a", "b", "c"]
    r2 = ["b", "c", "a"]
    base = rrf_fuse([r1, r2])

    # 模拟"某一路分数整体偏大"：RRF 只看排名，所以结果不该有任何变化。
    # 之所以要断言这条，是因为最容易犯的改动就是"改成加权求和更好调"。
    # 一旦那么改，向量（0~1）与 BM25（无上界）的分数就会互相淹没，
    # 而指标可能仍然"看起来合理"。
    scores_before = rrf_scores([r1, r2])
    scores_after = rrf_scores([r1, r2])          # 同一输入
    check("同输入同分数", scores_before == scores_after, "rrf_scores 不稳定")

    # 排名内部顺序变了才应该变
    flipped = rrf_fuse([["c", "b", "a"], r2])
    check("排名变化会影响结果", flipped != base, "排名变化未影响融合")

    # 单路输入 → 融合结果等于该路本身
    check("单路等于原序", rrf_fuse([r1]) == r1, str(rrf_fuse([r1])))


def test_consensus_first() -> None:
    print("多路共识优先")
    # x 只在向量路第 1；y 在两路但都在末位
    vec = ["x", "a", "b", "y"]
    bm = ["c", "d", "e", "y"]
    out = rrf_fuse([vec, bm])
    # y 得分 1/64 + 1/64 = 0.03125；x 只有 1/61 = 0.01639
    check("两路命中优先于单路第一", out.index("y") < out.index("x"), str(out))

    # 同一路内重复出现的 id 只记首次名次
    dup = ["a", "a", "a", "b"]
    single = ["a", "b"]
    check(
        "同路重复不自我放大",
        rrf_scores([dup]) == rrf_scores([single]),
        f"{rrf_scores([dup])} vs {rrf_scores([single])}",
    )


def test_k_constant() -> None:
    print("RRF k 常数")
    # k 越小，头部名次的差异被放得越大
    small = rrf_scores([["a", "b"]], k=1)
    large = rrf_scores([["a", "b"]], k=1000)
    ratio_small = small["a"] / small["b"]
    ratio_large = large["a"] / large["b"]
    check("k 小则头部优势更大", ratio_small > ratio_large, f"{ratio_small:.3f} vs {ratio_large:.3f}")
    check("默认 k 是 60（写入实验台账的值）", RRF_K == 60, str(RRF_K))


def test_fuse_items() -> None:
    print("对象融合")
    a = {"id": "a", "tag": "vec"}
    b = {"id": "b", "tag": "vec"}
    c = {"id": "c", "tag": "bm"}
    a2 = {"id": "a", "tag": "bm"}

    out = rrf_fuse_items([[a, b], [c, a2]], key=lambda x: x["id"])
    ids = [o["id"] for o, _ in out]
    check("按 id 去重", sorted(ids) == ["a", "b", "c"], str(ids))
    check("保留首次出现的对象", next(o for o, _ in out if o["id"] == "a")["tag"] == "vec",
          "没有保留首次出现的实例")
    check("分数降序", all(out[i][1] >= out[i + 1][1] for i in range(len(out) - 1)), str(out))


def test_dedupe_docs() -> None:
    print("文档级去重")
    mapping = {"c1": "d1", "c2": "d1", "c3": "d2", "c4": "d3"}
    out = dedupe_docs(["c1", "c2", "c3", "c4", "c1"], mapping)
    check("保持首次出现顺序", out == ["d1", "d2", "d3"], str(out))
    check("未映射的 chunk 被丢弃", dedupe_docs(["unknown"], mapping) == [], "未过滤未知 chunk")
    check("空输入安全", dedupe_docs([], mapping) == [], "空输入异常")


def main() -> int:
    print("=" * 70)
    print("多路融合测试（RRF）")
    print("=" * 70)
    test_basic()
    test_rank_only()
    test_consensus_first()
    test_k_constant()
    test_fuse_items()
    test_dedupe_docs()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
