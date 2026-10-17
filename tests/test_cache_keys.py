"""缓存纯逻辑层的单元测试。


运行方式（两种都行）：
    pytest tests/test_cache_keys.py -v
    python -m tests.test_cache_keys        # 无 pytest 时的降级跑法
"""

from app.cache.guards import check_anchor_consistency, check_length_ratio
from app.cache.keys import (
    cn_to_int,
    exact_cache_key,
    extract_anchors,
    normalize,
)


# ---------------------------------------------------------------- 中文数字


def test_cn_to_int_basic():
    assert cn_to_int("十六") == 16
    assert cn_to_int("二十") == 20
    assert cn_to_int("十") == 10
    assert cn_to_int("两") == 2
    assert cn_to_int("三") == 3


def test_cn_to_int_compound():
    assert cn_to_int("一百零八") == 108
    assert cn_to_int("三十") == 30
    assert cn_to_int("二百五十六") == 256


def test_cn_to_int_rejects_non_numeric():
    assert cn_to_int("") is None
    assert cn_to_int("民法典") is None


# ---------------------------------------------------------------- 归一化


def test_normalize_strips_politeness_and_punctuation():
    a = normalize("请问，诉讼时效是几年？")
    b = normalize("诉讼时效是几年")
    assert a == b


def test_normalize_collapses_whitespace_and_fullwidth():
    assert normalize("诉讼  时效 是几年") == normalize("诉讼 时效 是几年")
    assert normalize("ＡＢＣ") == "abc"


def test_normalize_converts_cn_number_before_measure_word():
    assert normalize("十六周岁以上的未成年人") == "16周岁以上的未成年人"
    assert normalize("满二年") == "满2年"


def test_normalize_does_not_touch_cn_number_without_measure_word():
    # "统一" / "一并" 不是数量表达，绝不能被改写
    assert "一" in normalize("统一标准")
    assert "一" in normalize("一并处理")


# ---------------------------------------------------------------- 锚点抽取


def test_extract_anchors_on_age():
    assert extract_anchors("我今年16岁，能独立签合同吗") == frozenset({"16"})
    assert extract_anchors("我今年十八岁，能独立签合同吗") == frozenset({"18"})


def test_extract_anchors_empty_for_text_only_question():
    assert extract_anchors("诉讼时效是几年") == frozenset()


# ---------------------------------------------------------------- 护栏


def test_guard_rejects_different_numbers():
    """这是语义缓存最危险的失效模式，必须拦住。"""
    result = check_anchor_consistency(
        "我今年18岁，能独立签合同吗", "我今年16岁，能独立签合同吗"
    )
    assert result.accepted is False
    assert "anchor_mismatch" in result.reason


def test_guard_accepts_same_number():
    result = check_anchor_consistency(
        "我今年16岁靠自己打工养活自己，能独立签合同吗", "16岁能独立签合同吗"
    )
    assert result.accepted is True


def test_guard_accepts_when_both_have_no_anchor():
    result = check_anchor_consistency("诉讼时效是几年", "一般诉讼时效多长")
    assert result.accepted is True


def test_guard_rejects_when_only_one_side_has_anchor():
    """一方多出约束条件，说明是不同的问题，保守拒绝。"""
    result = check_anchor_consistency("16岁能签合同吗", "能签合同吗")
    assert result.accepted is False


def test_length_ratio_guard():
    assert check_length_ratio("诉讼时效是几年", "诉讼时效多久").accepted is True
    long_q = "诉讼时效" * 60
    assert check_length_ratio(long_q, "诉讼时效多久").accepted is False


# ---------------------------------------------------------------- cache key


def test_exact_key_is_stable_and_normalization_insensitive():
    kwargs = dict(kb_version=1, prompt_version="v1", model="deepseek-chat", top_k=5)
    k1 = exact_cache_key(query="请问，诉讼时效是几年？", **kwargs)
    k2 = exact_cache_key(query="诉讼时效是几年", **kwargs)
    assert k1 == k2


def test_exact_key_changes_with_kb_version():
    """知识库更新后版本号 +1，旧 key 立即不可达 —— 这就是强失效。"""
    base = dict(prompt_version="v1", model="deepseek-chat", top_k=5)
    k1 = exact_cache_key(query="诉讼时效是几年", kb_version=1, **base)
    k2 = exact_cache_key(query="诉讼时效是几年", kb_version=2, **base)
    assert k1 != k2


def test_exact_key_changes_with_model_and_prompt_version():
    base = dict(kb_version=1, model="deepseek-chat", top_k=5)
    assert exact_cache_key(query="q", prompt_version="v1", **base) != exact_cache_key(
        query="q", prompt_version="v2", **base
    )
    base2 = dict(kb_version=1, prompt_version="v1", top_k=5)
    assert exact_cache_key(query="q", model="deepseek-chat", **base2) != exact_cache_key(
        query="q", model="deepseek-reasoner", **base2
    )


def _run_all():
    passed = 0
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
            except AssertionError as exc:
                failed.append((name, repr(exc)))
    print(f"\n{'=' * 52}")
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for name, err in failed:
        print(f"  FAIL  {name}: {err}")
    print("=" * 52)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
