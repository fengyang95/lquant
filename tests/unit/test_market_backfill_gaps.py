"""market.backfill 内部链路覆盖补齐：日历、库内已有、拉取与快照降级（全打桩）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.market import backfill as bf
from lquant.market.collectors.index_daily import INDEX_POOL

_CAL = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]


def _index_frame(dates, symbols=None):
    return pl.DataFrame([
        {"trade_date": d, "symbol": s, "name": "x", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "pre_close": 1.0, "volume": 1.0,
         "amount": 1.0, "collected_at": None}
        for s in (symbols or INDEX_POOL) for d in dates
    ])


class _Leaf:
    name = "leaf"


class _Chain:
    name = "fallback"
    providers = [_Leaf()]


class _Plain:
    name = "plain"


def test_target_provider_unwraps_fallback_chain(monkeypatch) -> None:
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: _Chain())
    assert bf._target_provider().name == "leaf"


def test_target_provider_plain_provider(monkeypatch) -> None:
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda: _Plain())
    assert bf._target_provider().name == "plain"


def test_trade_calendar_filters_open_and_range(monkeypatch) -> None:
    df = pl.DataFrame({
        "trade_date": _CAL,
        "is_open": [1, 0, 1, 1, 1, 1, 1, 1, 1, 1],
    })
    monkeypatch.setattr(bf, "_target_provider",
                        lambda: type("P", (), {
                            "trade_calendar": lambda self, s, e: df})())
    out = bf._trade_calendar(date(2026, 9, 1), date(2026, 9, 10))
    assert out == [d for i, d in enumerate(_CAL) if i != 1]  # is_open=0 被滤掉


def test_trade_calendar_empty_raises(monkeypatch) -> None:
    monkeypatch.setattr(bf, "_target_provider",
                        lambda: type("P", (), {
                            "trade_calendar": lambda self, s, e: pl.DataFrame()})())
    with pytest.raises(RuntimeError, match="交易日历不可用"):
        bf._trade_calendar(date(2026, 9, 1), date(2026, 9, 2))


def test_trade_calendar_bad_columns_raises(monkeypatch) -> None:
    monkeypatch.setattr(bf, "_target_provider",
                        lambda: type("P", (), {
                            "trade_calendar": lambda self, s, e: pl.DataFrame({"x": [1]})})())
    with pytest.raises(RuntimeError, match="交易日历列异常"):
        bf._trade_calendar(date(2026, 9, 1), date(2026, 9, 2))


def test_index_have_groups_by_symbol(monkeypatch) -> None:
    rows = [("sh000001", date(2026, 9, 1)), ("sh000001", date(2026, 9, 2)),
            ("sh000300", date(2026, 9, 1))]

    class _Con:
        def execute(self, sql):
            return type("R", (), {"fetchall": lambda self: rows})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import contextlib

    import lquant.core.db

    @contextlib.contextmanager
    def fake_reader():
        yield _Con()

    # _index_have 内部 `from lquant.core.db import reader` → 打桩源模块属性
    monkeypatch.setattr(lquant.core.db, "reader", fake_reader)
    have = bf._index_have()
    assert have["sh000001"] == {date(2026, 9, 1), date(2026, 9, 2)}
    assert have["sh000300"] == {date(2026, 9, 1)}


def test_persist_index_returns_count(monkeypatch) -> None:
    captured = {}

    def fake_persist(frames):
        captured["frames"] = frames
        return {"index_daily": 7}

    monkeypatch.setattr("lquant.market.scheduler.persist", fake_persist)
    df = _index_frame(_CAL[:2])
    assert bf._persist_index(df) == 7
    assert list(captured["frames"]) == ["index_daily"]


def test_fetch_index_delegates(monkeypatch) -> None:
    calls = {}

    def fake_fetch(*, start, end, demo):
        calls.update(start=start, end=end, demo=demo)
        return _index_frame(_CAL[:2])

    monkeypatch.setattr("lquant.market.collectors.fetch_index_daily", fake_fetch)
    df = bf._fetch_index(date(2026, 9, 1), "2026-09-03", demo=True)
    assert len(df) == 2 * len(INDEX_POOL)
    assert calls == {"start": date(2026, 9, 1).isoformat(),
                     "end": "2026-09-03", "demo": True}


def test_snapshot_coverage_handles_missing_table(monkeypatch) -> None:
    calls = []

    class _Con:
        def execute(self, sql):
            calls.append(sql)
            if "sector_daily" in sql:
                return type("R", (), {"fetchone": lambda self: (
                    date(2026, 9, 1), date(2026, 9, 2), 2)})()
            raise RuntimeError("table not found")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import contextlib

    import lquant.core.db

    @contextlib.contextmanager
    def fake_reader():
        yield _Con()

    monkeypatch.setattr(lquant.core.db, "reader", fake_reader)
    out = bf._snapshot_coverage()
    assert out["sector_daily"]["days"] == 2
    assert out["sentiment_daily"] == {"min_date": None, "max_date": None, "days": 0}


def test_ensure_market_coverage_no_missing(monkeypatch) -> None:
    monkeypatch.setattr(bf, "_trade_calendar", lambda start, end: _CAL)
    monkeypatch.setattr(bf, "_index_have",
                        lambda: {s: set(_CAL) for s in INDEX_POOL})
    report = bf.ensure_market_coverage(days=30)
    assert report["fetched"] == 0 and report["persisted"] == 0
    assert report["missing_before"] == {}
    assert report["missing_after"] == {}
    assert report["days"] == 30
    assert set(report["snapshots"]) == {"sector_daily", "sentiment_daily"}


def test_ensure_market_coverage_backfills_and_residue(monkeypatch) -> None:
    monkeypatch.setattr(bf, "_trade_calendar", lambda start, end: _CAL)
    have_states = [
        {s: set() for s in INDEX_POOL},          # 补齐前：全缺
        {s: {_CAL[:2][0]} for s in INDEX_POOL},  # 补齐后：仍缺第 1 天以外的
    ]
    monkeypatch.setattr(bf, "_index_have", lambda: have_states.pop(0))
    monkeypatch.setattr(bf, "_fetch_index",
                        lambda start, end, *, demo: _index_frame(_CAL[:1]))
    monkeypatch.setattr(bf, "_persist_index", lambda df: 1 * len(INDEX_POOL))
    report = bf.ensure_market_coverage(days=90, demo=True)
    assert report["fetched"] == len(INDEX_POOL)
    assert report["persisted"] == len(INDEX_POOL)
    # 补齐后仍有缺口 → missing_after 非空
    assert report["missing_after"]


def test_ensure_market_coverage_all_dates_today_or_future(monkeypatch) -> None:
    """日历全部 >= today → historical 为空 → 回退用整个日历（line 90）。"""
    import lquant.core.types as types_mod
    monkeypatch.setattr(bf, "_trade_calendar", lambda start, end: _CAL)
    monkeypatch.setattr(bf, "_index_have",
                        lambda: {s: set(_CAL) for s in INDEX_POOL})
    monkeypatch.setattr(bf, "now_cn",
                        lambda: types_mod.datetime(2026, 9, 1, 8, 0))
    report = bf.ensure_market_coverage(days=30)
    assert report["missing_before"] == {}
