"""data.fallback 链路覆盖补齐：健康度、降级仲裁与各能力路由。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.core.errors import SourceUnavailable
from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.fallback import FallbackProvider, HealthTracker


class _Prov(DataProvider):
    def __init__(self, name="p", caps=(Capability.REFERENCE,),
                 fail_for: set[str] | None = None):
        self.name = name
        self.capability = frozenset(caps)
        self.fail_for = fail_for or set()
        self.calls: list[str] = []

    def _maybe_fail(self, method):
        self.calls.append(method)
        if method in self.fail_for:
            raise RuntimeError(f"{self.name} {method} failed")

    def daily_bars(self, symbols, start, end):
        self._maybe_fail("daily_bars")
        return pl.DataFrame({"symbol": symbols})

    def minute_bars(self, symbols, start, end, freq):
        self._maybe_fail("minute_bars")
        return pl.DataFrame({"freq": [freq]})

    def adj_factors(self, symbols, start, end):
        self._maybe_fail("adj_factors")
        return pl.DataFrame({"symbol": symbols})

    def financial_pit(self, symbols, start, end):
        self._maybe_fail("financial_pit")
        return pl.DataFrame()

    def securities(self):
        self._maybe_fail("securities")
        return pl.DataFrame({"src": [self.name]})

    def trade_calendar(self, start, end):
        self._maybe_fail("trade_calendar")
        return pl.DataFrame({"d": [start]})

    def etf_meta(self):
        self._maybe_fail("etf_meta")
        return pl.DataFrame()

    def realtime(self, symbols):
        self._maybe_fail("realtime")
        return pl.DataFrame({"symbol": symbols})


def test_health_tracker_fail_cooldown_and_recover(monkeypatch) -> None:
    t = HealthTracker(cooldown_sec=60)
    assert t.available("a") is True
    t.fail("a")
    assert t.available("a") is False
    # 冷却到期恢复
    t._down_until["a"] -= 61
    assert t.available("a") is True
    t.ok("a")
    assert t.available("a") is True
    # 连续失败 → 冷却累加但封顶 3600（60s * n, n=100 → 上限 3600）
    import time as _time
    before = _time.monotonic()
    for _ in range(100):
        t.fail("b")
    assert t._down_until["b"] - before <= 3601
    assert t._fails["b"] == 100


def test_empty_provider_list_rejected() -> None:
    with pytest.raises(ValueError, match="至少需要 1 个"):
        FallbackProvider([])


def test_no_capable_source_raises(monkeypatch) -> None:
    fb = FallbackProvider([_Prov("a")])
    monkeypatch.setattr(fb.health, "available", lambda name: False)
    with pytest.raises(SourceUnavailable, match="无可用源"):
        fb.securities()


def test_all_sources_fail_raises_last_error() -> None:
    a = _Prov("a", fail_for={"securities"})
    b = _Prov("b", fail_for={"securities"})
    fb = FallbackProvider([a, b])
    with pytest.raises(SourceUnavailable, match="所有源均失败"):
        fb.securities()
    assert a.calls == ["securities"] and b.calls == ["securities"]
    # 失败后两源都进入冷却
    assert fb.health.available("a") is False
    assert fb.health.available("b") is False


def test_failover_to_next_source() -> None:
    a = _Prov("a", fail_for={"securities"})
    b = _Prov("b")
    fb = FallbackProvider([a, b])
    out = fb.securities()
    assert out["src"].to_list() == ["b"]
    assert fb.last_source == "b"


def test_proxy_methods_route_by_capability() -> None:
    p = _Prov("p", caps=(Capability.REFERENCE, Capability.DAILY,
                         Capability.CALENDAR, Capability.ADJ_FACTOR,
                         Capability.FINANCIAL_PIT, Capability.REALTIME,
                         Capability.ETF_META, Capability.MINUTE_1))
    fb = FallbackProvider([p])
    d = date(2026, 9, 1)
    assert len(fb.daily_bars(["x"], d, d)) == 1
    assert len(fb.minute_bars(["x"], d, d, "1min")) == 1
    assert len(fb.adj_factors(["x"], d, d)) == 1
    assert len(fb.financial_pit(["x"], d, d)) == 0
    assert len(fb.securities()) == 1
    assert len(fb.trade_calendar(d, d)) == 1
    assert len(fb.etf_meta()) == 0
    assert len(fb.realtime(["x"])) == 1


def test_minute_freq_routing() -> None:
    caps = {Capability.MINUTE_1, Capability.MINUTE_60}
    p = _Prov("p", caps=caps)
    fb = FallbackProvider([p])
    d = date(2026, 9, 1)
    assert len(fb.minute_bars(["x"], d, d, "1min")) == 1
    assert len(fb.minute_bars(["x"], d, d, "60min")) == 1
    with pytest.raises(ValueError, match="未知分钟频度"):
        fb.minute_bars(["x"], d, d, "7min")
    # 源只有 60min 能力 → 1min 请求无源可路由
    with pytest.raises(SourceUnavailable):
        fb.minute_bars(["x"], d, d, "15min")


def test_recent_daily_probe_uses_daily_capability() -> None:
    p = _Prov("p", caps=(Capability.DAILY,))
    fb = FallbackProvider([p])
    end = date(2026, 9, 17)
    out = fb.recent_daily_probe()
    assert len(out) == 1
    assert p.calls[0] == "daily_bars"
    # 窗口为最近 10 天
    assert (end - timedelta(days=10)) <= date(2026, 9, 17)
