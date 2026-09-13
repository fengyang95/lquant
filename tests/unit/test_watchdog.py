"""watchdog 子进程封装：大载荷死锁回归 + 超时/异常路径。"""

from __future__ import annotations

import os
import time

import pytest

from lquant.data.watchdog import run_with_watchdog


def _big() -> list[str]:
    return ["x" * 128] * 2048  # ~256KB，远超管道缓冲


def _hang() -> None:
    time.sleep(30)


def _boom() -> None:
    raise ValueError("boom")


def _crash() -> None:
    os._exit(1)


def test_large_payload_not_deadlocked() -> None:
    """回归：结果超过管道缓冲（~64KB）时，必须先读队列再等进程退出。

    曾有 bug：父进程先 join() 再 get()，子进程 feeder 阻塞写管道等父进程
    读 → join 永远超时 → 正常结果被误杀成 TimeoutError。
    """
    out = run_with_watchdog(_big, timeout=60)
    assert len(out) == 2048
    assert out[0] == "x" * 128


def test_timeout_raises() -> None:
    with pytest.raises(TimeoutError):
        run_with_watchdog(_hang, timeout=1)


def test_child_error_raises_runtime() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        run_with_watchdog(_boom, timeout=30)


def test_child_crash_without_result() -> None:
    with pytest.raises(RuntimeError, match="异常退出"):
        run_with_watchdog(_crash, timeout=30)
