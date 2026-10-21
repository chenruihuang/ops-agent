"""reconcile_stdout 的编码自愈只该修"真坏了"的场景。

背景（真实事故）：trace_doctor 在 PS 5.1 直连控制台跑，输出乱码 —— 而
"自愈"的 info 行说它已经 re-encoded 过了。根因：直连控制台时 stdout 底层是
`_WindowsConsoleIO`，Python 用 WriteConsoleW 宽字符直写，中文**本来正常**；
reconcile 只看 `sys.stdout.encoding == 'utf-8'` 就 reconfigure 成 cp936，
把 GBK 字节塞给按 UTF-8 解码的控制台接口 —— 自愈脚本自己成了乱码来源。

这三个用例把三种场景钉死，防止下次重构又把判断简化回"只看 encoding"。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from scripts import selftest


class _Raw:
    """模拟 FileIO（pipe / 重定向）：字节按编码写出，reconfigure 是有效的。"""


class _WindowsConsoleIO:
    """与 CPython 内部类型同名：reconcile 靠 type(raw).__name__ 识别它。"""


class _Buffer:
    def __init__(self, raw: Any) -> None:
        self.raw = raw


class _FakeStdout:
    def __init__(self, raw: Any, encoding: str = "utf-8") -> None:
        self.buffer = _Buffer(raw)
        self.encoding = encoding
        self.reconfigure_calls: list[dict] = []

    def reconfigure(self, **kwargs: Any) -> None:
        self.reconfigure_calls.append(kwargs)

    def isatty(self) -> bool:
        return False


class _FakeStderr(_FakeStdout):
    """logging 的输出通道。reconcile 成功时应一并降级（警告行也要能读）。"""


@pytest.fixture()
def fake_env(monkeypatch: pytest.MonkeyPatch):
    """把 parent_shell / console_codepage 钉成 PS 5.1 + 码页 936。"""

    def install(shell: str = "powershell.exe", cp: int | None = 936) -> None:
        monkeypatch.setattr(selftest, "parent_shell", lambda: shell)
        monkeypatch.setattr(selftest, "console_codepage", lambda: cp)

    return install


def test_console_widechar_path_is_left_alone(fake_env, monkeypatch) -> None:
    """直连控制台（宽字符路径）：中文本来就正常，stdout / stderr 都不许碰。"""
    fake_env()
    fake_out = _FakeStdout(_WindowsConsoleIO())
    fake_err = _FakeStderr(_WindowsConsoleIO())
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    assert selftest.reconcile_stdout() == ""
    assert fake_out.reconfigure_calls == []          # 一个字节都不许动
    assert fake_err.reconfigure_calls == []


def test_pipe_with_utf8_gets_downgraded(fake_env, monkeypatch) -> None:
    """管道 + PYTHONUTF8 场景：这正是当初要修的 Tee-Object 乱码，必须降级到 cp936。"""
    fake_env()
    fake_out = _FakeStdout(_Raw(), encoding="utf-8")
    fake_err = _FakeStderr(_Raw(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    note = selftest.reconcile_stdout()
    assert "cp936" in note
    assert fake_out.reconfigure_calls == [{"encoding": "cp936", "errors": "replace"}]
    assert fake_err.reconfigure_calls == [{"encoding": "cp936", "errors": "replace"}]


def test_non_decoding_host_is_untouched(fake_env, monkeypatch) -> None:
    """pwsh 7 / bash 按 UTF-8 解码：降级反而制造乱码，必须不干预。"""
    fake_env(shell="pwsh.exe")
    fake_out = _FakeStdout(_Raw(), encoding="utf-8")
    fake_err = _FakeStderr(_Raw(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    assert selftest.reconcile_stdout() == ""
    assert fake_out.reconfigure_calls == []
    assert fake_err.reconfigure_calls == []


def test_stderr_failure_does_not_break_stdout(fake_env, monkeypatch) -> None:
    """stderr 的 reconfigure 抛异常：stdout 已修好的结果不能被连坐。"""

    class _BrokenStderr(_FakeStderr):
        def reconfigure(self, **kwargs: Any) -> None:
            raise OSError("console gone")

    fake_env()
    fake_out = _FakeStdout(_Raw(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", _BrokenStderr(_Raw(), encoding="utf-8"))

    note = selftest.reconcile_stdout()
    assert "cp936" in note
    assert fake_out.reconfigure_calls == [{"encoding": "cp936", "errors": "replace"}]
