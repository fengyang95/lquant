"""em_client 限流代理单元测试（requests 打桩，不联网）。"""

from __future__ import annotations

import sys
import types

import pytest

from lquant.market import em_client


@pytest.fixture
def fake_requests(monkeypatch):
    """注入 fake requests 模块；清空 bucket 缓存保证隔离。"""
    mod = types.ModuleType("requests")
    mod.get = lambda url, **kw: "resp"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "requests", mod)
    em_client._bucket.cache_clear()
    yield mod
    em_client._bucket.cache_clear()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(em_client.time, "sleep", lambda s: None)


def test_success_with_defaults(fake_requests, no_sleep):
    recorded = []
    fake_requests.get = lambda url, **kw: (recorded.append((url, kw)), "resp")[1]
    r = em_client.em_get("http://x", qps=100)  # qps>=100 跳过限流等待
    assert r == "resp"
    url, kw = recorded[0]
    assert url == "http://x"
    assert kw["timeout"] == 15
    assert kw["headers"]["User-Agent"] == "Mozilla/5.0"


def test_explicit_kwargs_not_overridden(fake_requests, no_sleep):
    recorded = []
    fake_requests.get = lambda url, **kw: (recorded.append(kw), "resp")[1]
    em_client.em_get("http://x", qps=100, timeout=5, headers={"User-Agent": "ua"})
    assert recorded[0]["timeout"] == 5
    assert recorded[0]["headers"] == {"User-Agent": "ua"}


def test_retry_then_success(fake_requests, no_sleep):
    calls = {"n": 0}

    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("boom")
        return "ok"

    fake_requests.get = flaky
    assert em_client.em_get("http://x", qps=100) == "ok"
    assert calls["n"] == 3


def test_exhausted_retries_raise(fake_requests, no_sleep):
    fake_requests.get = lambda url, **kw: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(RuntimeError, match="请求失败"):
        em_client.em_get("http://x", qps=100)


def test_retry_sleep_backoff(fake_requests, no_sleep, monkeypatch):
    sleeps = []
    monkeypatch.setattr(em_client.time, "sleep", lambda s: sleeps.append(s))
    fake_requests.get = lambda url, **kw: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(RuntimeError):
        em_client.em_get("http://x", qps=100)
    assert sleeps == [1.0, 2.0]  # 2**i，最后一次失败直接抛不再空等 4s


def test_bucket_cached_per_qps(fake_requests):
    b1 = em_client._bucket(3.0)
    b2 = em_client._bucket(3.0)
    b3 = em_client._bucket(5.0)
    assert b1 is b2
    assert b1 is not b3
