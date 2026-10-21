"""评估产物快照：发布 → 落盘 → 重启回填。

为什么需要这条用例
------------------
`rag_context_recall` 这类 gauge 是**进程内**的。`docker compose up -d api`
一次就把它清零，而面板变空和"从没跑过评估"在人眼上完全一样 ——
实测踩过：刚 `--publish` 完是 0.8785，重建镜像后三个格子全空。

这里只测"落盘/回填"这一层（纯文件 + gauge），不启动服务：
`call_snapshot` 保证越界值不会被回填（1.3133 那种 AP 口径 bug 不该被复活）。
"""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.observability import metrics as M


@pytest.fixture()
def snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "observability_trace_dir", str(tmp_path))
    return tmp_path


def test_persist_then_restore(snapshot_dir) -> None:
    path = M.persist_eval(
        {"context_recall": 0.8785, "context_precision": None, "anchor_hit_rate": 0.8237},
        top_k=5,
    )
    assert path.exists()
    stored = json.loads(path.read_text(encoding="utf-8"))
    # None 不落盘："没测过"不能变成 0
    assert "context_precision" not in stored["values"]
    assert stored["top_k"] == 5

    restored = M.restore_eval()
    assert set(restored) == {"context_recall", "anchor_hit_rate"}
    assert M.CONTEXT_RECALL.labels(top_k="5")._value.get() == pytest.approx(0.8785)


def test_restore_rejects_out_of_range(snapshot_dir) -> None:
    """历史产物里出现 >1 的比例（AP 未按文档去重的 1.3133）不能被回填。"""
    path = M.eval_snapshot_path()
    path.write_text(
        json.dumps({"top_k": 5, "values": {"context_precision": 1.3133, "context_recall": 0.9}}),
        encoding="utf-8",
    )
    restored = M.restore_eval()
    assert restored == ["context_recall"]


def test_restore_missing_file_is_silent(snapshot_dir) -> None:
    assert M.restore_eval() == []
