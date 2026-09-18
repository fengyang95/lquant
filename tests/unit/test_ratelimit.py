"""ratelimit 令牌桶单元测试（time 打桩，不真等）。"""

from __future__ import annotations

import threading

import pytest

from lquant.data import ratelimit


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def patch_time(monkeypatch, clock: FakeClock) -> None:
    monkeypatch.setattr(ratelimit.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ratelimit.time, "sleep", clock.sleep)


def test_init_clamps_qps():
    b = ratelimit.TokenBucket(0.0)
    assert b.qps == 0.01
    b2 = ratelimit.TokenBucket(-5)
    assert b2.qps == 0.01


def test_init_capacity():
    b = ratelimit.TokenBucket(3.0)
    assert b.capacity == 3 and b.tokens == 3.0
    b2 = ratelimit.TokenBucket(0.5)  # int(qps)=0 → burst 默认 1
    assert b2.capacity == 1
    b3 = ratelimit.TokenBucket(2.0, burst=10)
    assert b3.capacity == 10


def test_high_qps_is_unlimited(monkeypatch):
    clock = FakeClock()
    patch_time(monkeypatch, clock)
    b = ratelimit.TokenBucket(100)
    for _ in range(100):
        b.acquire(50)
    assert clock.sleeps == []


def test_acquire_within_burst_no_sleep(monkeypatch):
    clock = FakeClock()
    patch_time(monkeypatch, clock)
    b = ratelimit.TokenBucket(5.0)
    for _ in range(5):
        b.acquire()
    assert clock.sleeps == []


def test_acquire_refills_over_time(monkeypatch):
    clock = FakeClock()
    patch_time(monkeypatch, clock)
    b = ratelimit.TokenBucket(10.0)
    for _ in range(10):  # 清空桶
        b.acquire()
    clock.now += 0.5  # 补 5 个 token
    b.acquire(5)
    assert clock.sleeps == []


def test_acquire_blocks_when_empty(monkeypatch):
    clock = FakeClock()
    patch_time(monkeypatch, clock)
    b = ratelimit.TokenBucket(10.0, burst=2)
    b.acquire(2)  # 桶空
    b.acquire(2)  # 需要等 0.2s
    assert clock.sleeps == [pytest.approx(0.2)]
    assert clock.now > 1000.0


def test_tokens_capped_at_capacity(monkeypatch):
    clock = FakeClock()
    patch_time(monkeypatch, clock)
    b = ratelimit.TokenBucket(10.0, burst=3)
    clock.now += 100  # 巨量补充也不超过容量
    b.acquire(3)
    assert b.tokens == 0.0


def test_acquire_n_le_capacity_refills_via_sleep(monkeypatch):
    # n <= capacity 时，靠 sleep 攒 token 后返回；n > capacity 会死循环（已上报源码）
    clock = FakeClock()
    patch_time(monkeypatch, clock)
    b = ratelimit.TokenBucket(50.0, burst=10)
    b.acquire(10)  # 第一次直接放行，桶清空
    b.acquire(10)  # 第二次需 sleep 攒 token
    assert b.tokens == 0.0
    assert clock.sleeps


def test_thread_safety_smoke():
    b = ratelimit.TokenBucket(100)  # 不限流路径，纯并发冒烟
    threads = [threading.Thread(target=b.acquire, args=(10,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_acquire_over_capacity_raises():
    """n > capacity：直接 ValueError（原先会死循环自旋）。"""
    from lquant.data.ratelimit import TokenBucket

    tb = TokenBucket(qps=5, burst=10)
    with pytest.raises(ValueError):
        tb.acquire(11)
