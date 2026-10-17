"""`scripts/bench_rerank.py` 里那两段纯逻辑的单测。

为什么值得单独测这两段
----------------------
它们回答的是"**该按多大的候选池换算单题耗时**"。这个问题的错误答案是
静默的：换算出一个偏乐观的数字，不报错、不告警，也没有任何测试会红 ——
因为错的那个数字（比如写死的 20）从来没进过业务代码，
它只活在 benchmark 的参数默认值里。

本项目真实踩过这个坑：按 20 对报的成本，而线上实测中位是 **29** 块，
**低估约 1.45 倍**。所以这里把两段逻辑钉住：

- `merge_specs`：实测到的池子必须**真的被加进测量档位**。
  漏掉 append 的后果是"还是按手填档位换算"—— 而它看起来完全正常，
  这正是它危险的地方。
- `pick_serving`：换算必须挑**实测中位**那一行，不能想当然用 `rows[-1]`
  （那是"我指定的最大档位"，不等同于线上真实规模）。

两段都是纯函数（不加载模型、不连向量库），所以这套用例是离线秒级的。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.bench_rerank import merge_specs, pick_serving  # noqa: E402

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


# =====================================================================
def test_merge_specs_appends_observed_median() -> None:
    """实测中位不在手填档位里时，必须被加进去。"""
    print("merge_specs：实测中位要进档位")
    specs = merge_specs("1,5,20", {"median": 29})
    check("实测中位被加入", 29 in specs, str(specs))
    check("手填档位保留", [1, 5, 20] == specs[:3], str(specs))
    check("结果有序", specs == sorted(specs), str(specs))
    check("没有重复", len(specs) == len(set(specs)), str(specs))


def test_merge_specs_without_observed() -> None:
    """没有实测值时就用手填档位，且不塞脏数据。"""
    print("merge_specs：无实测值时不塞脏数据")
    check("用手填档位", merge_specs("1,5,20", None) == [1, 5, 20],
          str(merge_specs("1,5,20", None)))
    check("不引入 0", 0 not in merge_specs("1,5,20", None),
          str(merge_specs("1,5,20", None)))
    # median=0 意味着"空样本"或"没测出来"—— 0 对的重排没有意义，不该进档位
    check("median=0 不塞入", merge_specs("1,5", {"median": 0}) == [1, 5],
          str(merge_specs("1,5", {"median": 0})))


def test_merge_specs_dedupes() -> None:
    """实测中位恰好等于手填档位时不重复添加。"""
    print("merge_specs：去重")
    check("不重复", merge_specs("1,5,20", {"median": 20}) == [1, 5, 20],
          str(merge_specs("1,5,20", {"median": 20})))


def test_pick_serving_uses_observed_median() -> None:
    """换算行必须按实测中位挑，不是按最大档位。

    刻意构造"最大档位 != 实测中位"的数据：
    如果实现写成 `rows[-1]`，它会挑到 50 那一行，
    于是单题耗时被换算成 50 对的值 —— 这正是坑本身。
    """
    print("pick_serving：按实测中位挑，不按 rows[-1]")
    rows = [
        {"pairs": 1, "median_seconds": 5.0},
        {"pairs": 20, "median_seconds": 30.0},
        {"pairs": 29, "median_seconds": 36.0},
        {"pairs": 50, "median_seconds": 50.0},
    ]
    served = pick_serving(rows, {"median": 29})
    check("挑到实测中位那一行", served["pairs"] == 29, str(served))
    check("不是最大档位（rows[-1] 是错误答案）", served["pairs"] != 50, str(served))
    check("耗时取自实测那一行", served["median_seconds"] == 36.0, str(served))


def test_pick_serving_fallback() -> None:
    """没有实测值时退回最大档位；实测档位缺失时不崩。"""
    print("pick_serving：兜底与不崩")
    rows = [{"pairs": 1, "median_seconds": 5.0},
            {"pairs": 20, "median_seconds": 30.0}]
    check("无实测 → 最大档位", pick_serving(rows, None)["pairs"] == 20,
          str(pick_serving(rows, None)))
    back = pick_serving(rows, {"median": 99})
    check("实测档位缺失 → 退回而非抛异常", back["pairs"] == 20, str(back))


def test_pick_serving_empty_rows_raises() -> None:
    """空 rows 必须显式报错，不能静默返回 None 让调用方在后面崩。"""
    print("pick_serving：空 rows 显式报错")
    try:
        pick_serving([], {"median": 29})
    except ValueError as exc:
        check("空 rows 抛 ValueError 且信息可读", "测量" in str(exc), str(exc))
    else:
        check("空 rows 应抛 ValueError", False, "没有抛异常")
