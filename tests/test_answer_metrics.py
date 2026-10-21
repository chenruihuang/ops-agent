"""答案侧指标的口径测试。

这些断言防的是**"看起来算了个分"**：
边界匹配、None 与 0 的区别、均值分母。三者任一错，报告的数就是错的，
而且不会报错 —— 只会安静地偏高或偏低。
"""

from __future__ import annotations

from app.eval.answer_metrics import (
    abstains,
    anchor_hit_rate,
    contains_anchor,
    doc_precision,
    doc_recall,
    normalize,
    summarize,
)

PASS = 0
FAIL = 0   # 必须是 int：conftest 的汇总按 `isinstance(FAIL, int)` 识别（见 tests/conftest.py）


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{'  ' + detail if detail else ''}")


def test_normalize() -> None:
    print("归一化：全角/千分位/大小写/空白")
    check("全角数字转半角", normalize("５０") == "50", normalize("５０"))
    check("千分位去掉", normalize("1,000ms") == "1000ms", normalize("1,000ms"))
    check("大小写不敏感", normalize("QPS") == "qps", normalize("QPS"))
    check("连续空白折叠", normalize("a  \u3000b") == "a b", repr(normalize("a  \u3000b")))
    check("空输入安全", normalize("") == "")


def test_anchor_boundary() -> None:
    print("锚点边界：子串命中是最常见的假阳性")
    check("整词命中", contains_anchor("单用户下单 QPS 限 5", "QPS"))
    check("数字不能命中选择性子串（5 不命中 15）",
          not contains_anchor("超时 15 秒", "5"))
    check("资产编码不能被更长编码命中（SRV-0088 不命中 SRV-00881）",
          not contains_anchor("资产 SRV-00881 在线", "SRV-0088"))
    check("资产编码正常命中", contains_anchor("资产 SRV-0088 在线", "SRV-0088"))
    check("错误码命中", contains_anchor("返回 ORD-429-001", "ord-429-001"))
    check("中文锚点命中", contains_anchor("触发熔断后快速失败", "熔断"))
    check("换行不影响", contains_anchor("限流\n为 5", "5"))


def test_anchor_hit_rate() -> None:
    print("命中率：None 与 0 必须分开")
    rate, hit, missed = anchor_hit_rate("QPS 限 5，返回 ORD-429-001", ["QPS", "5", "ORD-429-001"])
    check("三命中 → 1.0", rate == 1.0, str(rate))
    check("命中列表完整", hit == ["QPS", "5", "ORD-429-001"], str(hit))

    rate2, _, missed2 = anchor_hit_rate("QPS 限 50", ["QPS", "ORD-429-001"])
    check("答错错误码 → 0.5 而不是满分", rate2 == 0.5, str(rate2))
    check("漏掉的锚点被记录", missed2 == ["ORD-429-001"], str(missed2))

    empty, _, _ = anchor_hit_rate("任意答案", [])
    check("没有标注锚点 → None（不是 0）", empty is None, str(empty))
    blank, _, _ = anchor_hit_rate("", ["QPS"])
    check("空答案 → 0.0", blank == 0.0, str(blank))


def test_doc_metrics() -> None:
    print("文档级召回与 AP")
    check("全命中 → 1.0", doc_recall(["a", "b"], ["a", "x", "b"]) == 1.0)
    check("半命中 → 0.5", doc_recall(["a", "b"], ["a", "x"]) == 0.5)
    check("无 golden → None（不参与均值）", doc_recall([], ["a"]) is None)
    check("召回为空 → 0.0", doc_recall(["a"], []) == 0.0)

    # golden {a,b}，序列 [x,a,y,b]：precision@2=1/2、precision@4=2/4 → AP=(0.5+0.5)/2
    check("AP 计算（无关文档排前面会掉分）",
          abs(doc_precision(["a", "b"], ["x", "a", "y", "b"]) - 0.5) < 1e-9,
          str(doc_precision(["a", "b"], ["x", "a", "y", "b"])))
    # 顺序换过来：[a,b,x,y] → precision@1=1、precision@2=1 → AP=1.0
    check("相关文档排前面 → AP 1.0",
          doc_precision(["a", "b"], ["a", "b", "x", "y"]) == 1.0)
    # 同一篇的多个块会重复出现在 sources 里，必须先按首次出现去重；
    # 不去重时 AP 会算出 1.31/2.0 这种越界值（真实发生过）。
    check("重复召回去重后 AP 不超过 1",
          doc_precision(["a"], ["a", "a", "a"]) == 1.0,
          str(doc_precision(["a"], ["a", "a", "a"])))
    check("两篇 golden 都在，重复块不抬高 AP",
          doc_precision(["a", "b"], ["a", "a", "b", "b"]) == 1.0,
          str(doc_precision(["a", "b"], ["a", "a", "b", "b"])))
    check("一个都没召回 → 0.0", doc_precision(["a"], ["x", "y"]) == 0.0)


def test_abstain() -> None:
    print("弃答识别：不能把'没有超时'当成弃答")
    check("资料未涉及", abstains("知识库未涉及该问题"))
    check("没有找到", abstains("没有找到相关工单"))
    check("正常否定句不算弃答", not abstains("本次没有超时，P95 为 120ms"))
    check("正常答案不算弃答", not abstains("单用户下单 QPS 限 5"))


def test_summarize() -> None:
    print("汇总：分母必须跟着数字一起报")
    rows = [
        {"context_recall": 1.0, "context_precision": 1.0, "anchor_hit_rate": 1.0,
         "answer": "a", "degraded": False, "total_ms": 100.0, "ttft_ms": 50.0,
         "golden_doc_ids": ["d1"], "abstained": False, "intent": "rag"},
        {"context_recall": 0.0, "context_precision": 0.0, "anchor_hit_rate": None,
         "answer": "b", "degraded": True, "total_ms": 300.0, "ttft_ms": 150.0,
         "golden_doc_ids": ["d2"], "abstained": False, "intent": "rag"},
        {"context_recall": None, "context_precision": None, "anchor_hit_rate": None,
         "answer": "", "degraded": False, "total_ms": 5000.0, "ttft_ms": None,
         "golden_doc_ids": [], "abstained": False, "intent": "rag"},
        {"context_recall": None, "context_precision": None, "anchor_hit_rate": None,
         "answer": "资料未涉及该问题", "degraded": False, "total_ms": 800.0, "ttft_ms": 400.0,
         "golden_doc_ids": [], "abstained": True, "intent": "rag"},
        {"context_recall": None, "context_precision": None, "anchor_hit_rate": None,
         "answer": "建议现场排查", "degraded": False, "total_ms": 400.0, "ttft_ms": 200.0,
         "golden_doc_ids": [], "abstained": False, "intent": "rag"},
        # tool 题没有 golden 是因为答案来自实时系统，不该要求弃答 —— 不能进分母
        {"context_recall": None, "context_precision": None, "anchor_hit_rate": None,
         "answer": "工单处理中", "degraded": False, "total_ms": 60.0, "ttft_ms": 30.0,
         "golden_doc_ids": [], "abstained": False, "intent": "tool"},
    ]
    report = summarize(rows)
    check("recall 均值只对有值的题求", report["context_recall"] == 0.5, str(report["context_recall"]))
    check("recall 分母 = 2", report["context_recall_n"] == 2, str(report["context_recall_n"]))
    check("anchor 分母 = 1（None 不参与）", report["anchor_hit_rate_n"] == 1,
          str(report["anchor_hit_rate_n"]))
    # 分母是全部尝试题（6），不是有答案的题（5）—— 否则整轮失败会被算成 0% 降级
    check("degraded_rate 分母 = 全部尝试题（1/6）", report["degraded_rate"] == 0.1667,
          str(report["degraded_rate"]))
    # 弃答率只看"该弃答的题"：rag/hybrid 且无 golden。tool 题不算，有 golden 的题也不算
    # （后者的"未涉及"多半只是补充说明，算进去会让指标恒为 1.0 —— 真实踩过）。
    check("弃答率分母只含 rag 且无 golden 的题（3 题，含空答案）",
          report["unanswerable_n"] == 3, str(report["unanswerable_n"]))
    check("弃答率 = 1/3（空答案不算弃答成功）", report["abstention_ok_rate"] == 0.3333,
          str(report["abstention_ok_rate"]))
    # nearest-rank：有答案的四条 [100,300,800,400] → p50=300（第 2 个）、p95=800（第 4 个）。
    # 第三行故意放 5000ms（空答案）：若被算进来，p95 会变成 5000 —— 这才是能分辨的断言。
    check("空答案不计入 latency（p95=800 说明 5000ms 那条被排除）",
          report["total_ms_p95"] == 800.0, str(report["total_ms_p95"]))
    check("p50 用最近秩", report["total_ms_p50"] == 300.0,
          str(report["total_ms_p50"]))
    check("questions_total 保留全量", report["questions_total"] == 6, str(report["questions_total"]))


def main() -> int:
    print("=" * 70)
    print("答案侧指标测试")
    print("=" * 70)
    for fn in (test_normalize, test_anchor_boundary, test_anchor_hit_rate,
               test_doc_metrics, test_abstain, test_summarize):
        fn()
    print(f"\n{PASS} 条通过 · {FAIL} 条失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
