"""让 pytest 直接支持本仓库的 async 测试 —— 不依赖 pytest-asyncio。

为什么需要这个文件
------------------
本仓库的异步测试都是无参的 `async def test_xxx()`。pytest 本身不支持这种写法，
必须装 pytest-asyncio，或者用 anyio 并在每个测试上打 marker。
缺失时的表现很有误导性：46 个异步测试全部报
"async def functions are not natively supported"，看起来像"代码坏了"，
实际是"环境少个插件"。而 `pytest tests/` 恰恰是任何人复现的第一步 ——
一个默认跑不起来的测试套件，等于没有测试套件。

这里用一个 pytest hook 解决：凡是协程测试函数，交给 asyncio.run 执行。
只要 pytest-asyncio 已安装就主动让位（返回 None），避免两套机制重复执行。

同时补上仓库里一直被引用、却从未定义的 `tmp` fixture。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

try:  # pragma: no cover - 仅探测是否已有插件接管
    import pytest_asyncio  # noqa: F401

    _HAS_PYTEST_ASYNCIO = True
except ImportError:  # pragma: no cover
    _HAS_PYTEST_ASYNCIO = False


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    """本仓库统一使用的临时目录 fixture。

    test_indexing 等用例直接写了 `tmp: Path`，但仓库里从未定义过它 ——
    两个用例因此在收集阶段就报 "fixture 'tmp' not found"，
    而报错信息容易被误读成"测试写了但没跑通"。这里补上定义，
    语义就是 pytest 自带的 tmp_path，只是名字更短。
    """
    return tmp_path


def pytest_pyfunc_call(pyfuncitem):
    """把 async def 测试函数同步跑完。返回 True 表示已自行处理该用例。"""
    if _HAS_PYTEST_ASYNCIO:
        return None
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    argnames = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in argnames}
    asyncio.run(func(**kwargs))
    return True


# =====================================================================
# 让 check() 型的用例真正能失败
# =====================================================================
# 本仓库有一批用例（test_fusion / test_tools / test_intent）用的是
#     check("名字", 条件)  →  累加模块级 PASS / FAIL
# 然后由一个 main() 汇总并 `return 1 if FAIL`。
#
# 这个写法直接 `python -m tests.test_fusion` 跑是对的，但 **pytest 收集时
# 只跑那些 test_* 函数，没有任何东西去读 FAIL**。后果是：
#     check 失败了，pytest 依然报绿。
# 也就是说"90 passed"这个数字里，比例最大的那部分断言其实没被 gate 住 ——
# 一个 17 条断言全红的文件，在 CI 里和全绿长得一模一样。
#
# 这种"看起来有测试、实际不设防"的状态比没有测试更危险，因为它会让人
# 放心地改代码。所以这里在 session 结束时把各模块的 FAIL 汇总起来，
# 有任何一个非 0 就把整个 session 判为失败。
#
# 不用给每个 check() 改成 assert：那要动上百处，且会丢掉"一次跑完、
# 看全貌"的优点（本仓库的 check 风格刻意不在第一个失败处停下）。
def _check_counters() -> list[tuple[str, int, int]]:
    """收集所有 tests.* 模块的 (模块名, PASS, FAIL)。"""
    found: list[tuple[str, int, int]] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("tests."):
            continue
        passed = getattr(module, "PASS", None)
        failed = getattr(module, "FAIL", None)
        if isinstance(passed, int) and isinstance(failed, int):
            found.append((name, passed, failed))
    return sorted(found)


def pytest_sessionfinish(session, exitstatus):
    counters = _check_counters()
    if not counters:
        return

    total_pass = sum(p for _, p, _ in counters)
    total_fail = sum(f for _, _, f in counters)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    if total_fail:
        detail = " · ".join(f"{n}: {p}通过/{f}失败" for n, p, f in counters if f)
        message = (
            f"\ncheck() 型断言有 {total_fail} 条失败（共 {total_pass + total_fail} 条）：{detail}\n"
            "这些失败不会出现在 pytest 的用例计数里，但必须修 —— 见 conftest 的说明。"
        )
        if reporter is not None:
            reporter.write_line(message, red=True)
        # 把 session 判为失败。这是唯一能让 CI / `pytest` 退出码非 0 的位置。
        session.exitstatus = 1
    elif reporter is not None:
        reporter.write_line(
            f"check() 型断言：{total_pass} 条全部通过"
            f"（覆盖 {len(counters)} 个文件，已纳入 pytest 退出码）",
            green=True,
        )
