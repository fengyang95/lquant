"""任务进度注册表：流式进度条的数据源。"""
from __future__ import annotations

import time

from lquant.server import progress


def _clear() -> None:
    progress._PROGRESS.clear()
    progress._NAMES.clear()


def test_set_and_get_progress() -> None:
    _clear()
    progress.set_progress("j1", done=3, total=10, phase="计算因子")
    p = progress.get_progress("j1")
    assert p is not None
    assert {k: p[k] for k in ("done", "total", "phase", "message")} == {
        "done": 3, "total": 10, "phase": "计算因子", "message": None}


def test_get_progress_none_for_unknown() -> None:
    _clear()
    assert progress.get_progress("nope") is None


def test_progress_overwrite_keeps_latest() -> None:
    _clear()
    progress.set_progress("j1", done=1, total=10, phase="a")
    progress.set_progress("j1", done=2, total=10, phase="b")
    assert progress.get_progress("j1")["done"] == 2


def test_progress_eviction_when_over_capacity() -> None:
    _clear()
    for i in range(progress._PROGRESS_MAX + 10):
        progress.set_progress(f"j{i}", done=1, total=2, phase="p")
    assert len(progress._PROGRESS) <= progress._PROGRESS_MAX


def test_job_name_registry() -> None:
    _clear()
    progress.set_job_name("j1", "因子评价")
    assert progress.get_job_name("j1") == "因子评价"
    assert progress.get_job_name("j2") is None


def test_progress_message() -> None:
    _clear()
    progress.set_progress("j1", done=5, total=9, phase="评价", message="IC 计算完成")
    p = progress.get_progress("j1")
    assert p is not None and p["message"] == "IC 计算完成"


def test_progress_timestamp_present() -> None:
    _clear()
    before = time.time()
    progress.set_progress("j1", done=0, total=5, phase="x")
    p = progress.get_progress("j1")
    assert p is not None and p["ts"] >= before
