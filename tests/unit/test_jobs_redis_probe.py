"""`jobs._redis_available` 的 TTL 探测缓存。

回归点：它以前是 `@lru_cache(maxsize=1)`，首次探测结果被进程永久记住。于是
「API 比 Redis 先起来」这种常见部署下，首次 ping 失败就永久降级为本地线程，
Redis 随后就绪也不会切回 RQ。这里锁住三条不变量：
  1. TTL 内复用缓存（热路径不重复 ping）；
  2. TTL 过期 / ttl<=0 时重新探测；
  3. 探测结果可翻转（失败不粘滞）。
"""
from __future__ import annotations

import pytest

from lquant.server import jobs


@pytest.fixture(autouse=True)
def _reset_probe():
    """清掉模块级探测缓存，避免用例间相互污染。"""
    jobs._redis_probe_ok = None
    jobs._redis_probe_at = 0.0
    yield
    jobs._redis_probe_ok = None
    jobs._redis_probe_at = 0.0


def test_probe_result_is_memoized_within_ttl(monkeypatch):
    calls: list[int] = []

    def fake_probe() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(jobs, "_probe_redis", fake_probe)
    assert jobs._redis_available() is False
    assert jobs._redis_available() is False
    assert len(calls) == 1, "TTL 内应复用缓存，不重复 ping"


def test_ttl_zero_forces_fresh_probe(monkeypatch):
    calls: list[int] = []

    def fake_probe() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(jobs, "_probe_redis", fake_probe)
    assert jobs._redis_available(ttl=0) is True
    assert jobs._redis_available(ttl=0) is True
    assert len(calls) == 2, "ttl<=0 必须每次都重新探测"


def test_expired_ttl_reprobes(monkeypatch):
    calls: list[int] = []

    def fake_probe() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(jobs, "_probe_redis", fake_probe)
    assert jobs._redis_available(ttl=60) is True
    jobs._redis_probe_at = -1e9  # 把缓存做旧，模拟 TTL 过期
    assert jobs._redis_available(ttl=60) is True
    assert len(calls) == 2


def test_recovers_after_redis_comes_up(monkeypatch):
    """首次失败不粘滞 —— 旧 lru_cache 实现在这里会挂。"""
    state = {"up": False}
    monkeypatch.setattr(jobs, "_probe_redis", lambda: state["up"])

    assert jobs._redis_available(ttl=0) is False
    state["up"] = True
    assert jobs._redis_available(ttl=0) is True


def test_health_check_forces_fresh_probe(monkeypatch):
    """健康检查要报「此刻」的真实依赖状态，不吃探测缓存。"""
    from lquant.server.api import health

    seen: dict[str, float] = {}

    def fake(ttl: float = 30.0) -> bool:
        seen["ttl"] = ttl
        return False

    monkeypatch.setattr(jobs, "_redis_available", fake)
    out = health._redis_status()
    assert out["available"] is False
    assert seen["ttl"] == 0, "健康检查必须强制新鲜探测"
