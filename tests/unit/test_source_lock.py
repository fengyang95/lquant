"""源级单飞锁：限流管速率，它管并发会话 —— 两者正交，缺一个都会被源站封。

BaoStock 的黑名单错误码 ``10001011`` 触发条件明确包含「并发连接」，
所以「两个 worker 各自限流合规但同时在 login」必须被拦住。
"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time
import uuid

import pytest

from lquant.data import ratelimit
from lquant.data.ratelimit import source_lock


def _hold_lock(name: str, ready, release) -> None:  # pragma: no cover - 子进程
    with source_lock(name, timeout=5.0):
        ready.set()
        release.wait(10.0)


def test_nested_acquisition_does_not_deadlock():
    """同一进程内嵌套获取同一把锁必须安全（flock 只在首次进入时拿）。"""
    name = f"test-nested-{uuid.uuid4().hex}"
    with source_lock(name, timeout=1.0), source_lock(name, timeout=1.0):
        assert ratelimit._depth[name] == 2
    assert name not in ratelimit._depth


def test_in_process_threads_are_serialized():
    """同进程两个线程打同一个源：必须串行，不能重叠。"""
    name = f"test-thread-{uuid.uuid4().hex}"
    events: list[tuple[str, float]] = []

    def worker(tag: str) -> None:
        with source_lock(name, timeout=2.0):
            events.append((f"{tag}-in", time.monotonic()))
            time.sleep(0.15)
            events.append((f"{tag}-out", time.monotonic()))

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    time.sleep(0.02)      # 让 a 先拿到锁
    t2.start()
    t1.join(5)
    t2.join(5)

    by_tag = {tag: ts for tag, ts in events}
    # 后进入者的进入时刻必须晚于先进入者的退出时刻
    first_in = min(by_tag[k] for k in by_tag if k.endswith("-in"))
    second_in = max(by_tag[k] for k in by_tag if k.endswith("-in"))
    first_out = min(by_tag[k] for k in by_tag if k.endswith("-out"))
    assert second_in >= first_out - 1e-6, f"出现重叠执行: {events}"
    assert first_in < second_in


def test_cross_process_lock_times_out_instead_of_racing():
    """另一个进程持锁时，本进程**超时报错**而不是硬闯（并发会话会被封）。"""
    name = f"test-proc-{uuid.uuid4().hex}"
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(name, ready, release), daemon=True)
    p.start()
    try:
        assert ready.wait(30), "子进程没能拿到源锁"
        with pytest.raises(TimeoutError, match="等待源锁"), source_lock(name, timeout=0.4):
            pytest.fail("不应拿到锁：另一个进程仍持有")
    finally:
        release.set()
        p.join(10)
        if p.is_alive():  # pragma: no cover
            p.kill()


def test_cross_process_lock_released_after_exit():
    """子进程退出后锁释放，本进程可以正常拿到（不留下死锁）。"""
    name = f"test-proc2-{uuid.uuid4().hex}"
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(name, ready, release), daemon=True)
    p.start()
    assert ready.wait(30)
    release.set()
    p.join(10)
    with source_lock(name, timeout=5.0):
        pass


def _noop_fn() -> str:  # pragma: no cover - 子进程序列化要求模块级函数
    return "ok"


def test_watchdog_uses_source_lock(monkeypatch):
    """watchdog 是 Provider 网络调用的唯一出口 → 必须走同一把源锁。"""
    from lquant.data import watchdog

    name = f"test-wd-{uuid.uuid4().hex}"
    monkeypatch.setattr(watchdog, "_source_of", lambda fn: name)

    seen: list[str] = []
    orig = ratelimit.source_lock

    def _spy(src, timeout=ratelimit.DEFAULT_LOCK_TIMEOUT_SEC):
        seen.append(src)
        return orig(src, timeout)

    monkeypatch.setattr(ratelimit, "source_lock", _spy)
    assert watchdog.run_with_watchdog(_noop_fn, timeout=5) == "ok"
    assert seen == [name]


def test_source_of_derives_provider_name():
    """源名从模块名推断（Provider 的网络调用都是模块级函数）。"""
    from lquant.data.watchdog import _source_of

    def _fake_bs_query(day: str) -> list:  # pragma: no cover - 只取模块名
        return []

    _fake_bs_query.__module__ = "lquant.data.providers.baostock"
    assert _source_of(_fake_bs_query) == "baostock"
    # 推断不出源名时落到共享的 unknown 桶（宁可串行，也不要并发打同一个源）
    assert _source_of(object()) == "unknown"
