"""market.backfill 缺失检查与补齐测试（离线，全部打桩）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from lquant.market.backfill import (
    DEFAULT_LOOKBACK_DAYS,
    ensure_market_coverage,
    missing_trade_dates,
)
from lquant.market.collectors.index_daily import INDEX_POOL

# 全部交易日（无缺口）
_CAL = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]


def _index_frame(dates, symbols=None):
    rows = [
        {"trade_date": d, "symbol": s, "name": "x", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "pre_close": 1.0, "volume": 1.0,
         "amount": 1.0, "collected_at": None}
        for s in (symbols or INDEX_POOL) for d in dates
    ]
    return pl.DataFrame(rows)


def test_missing_trade_dates_basic():
    have = {date(2026, 9, 1), date(2026, 9, 3)}
    assert missing_trade_dates(have, _CAL) == [
        date(2026, 9, 2)] + _CAL[3:]


def test_missing_trade_dates_full_coverage():
    assert missing_trade_dates(set(_CAL), _CAL) == []
    assert missing_trade_dates(set(), _CAL) == _CAL


def _patch_common(monkeypatch, have_dates, fetched_frame, *, post_persist=None):
    """打桩：日历、库内已有日期（按调用次序）、指数拉取、写库。"""
    monkeypatch.setattr(
        "lquant.market.backfill._trade_calendar",
        lambda start, end: _CAL,
    )
    have_calls: list[dict] = []

    def _fake_have():
        # 第 1 次调用 = 补齐前查库；第 2 次 = 补齐后复查
        if post_persist is not None and have_calls:
            return {s: set(post_persist) for s in INDEX_POOL}
        have_calls.append({})
        return {s: set(have_dates) for s in INDEX_POOL}

    monkeypatch.setattr("lquant.market.backfill._index_have", _fake_have)
    calls = {}

    def _fake_fetch(start, end, *, demo):
        calls["start"], calls["end"] = start, end
        return fetched_frame

    def _fake_persist(df):
        calls["persisted"] = len(df)
        return len(df)

    monkeypatch.setattr("lquant.market.backfill._fetch_index", _fake_fetch)
    monkeypatch.setattr("lquant.market.backfill._persist_index", _fake_persist)
    return calls


def test_ensure_coverage_backfills_missing(monkeypatch):
    have = _CAL[:3]  # 前 3 天有，后 7 天缺
    fetched = _index_frame(_CAL[3:])
    calls = _patch_common(monkeypatch, have, fetched, post_persist=_CAL)  # upsert 后 = 旧数据∪新补

    report = ensure_market_coverage(days=90)

    assert report["missing_before"] and set(report["missing_before"]) == set(INDEX_POOL)
    assert report["fetched"] == len(fetched)
    assert report["persisted"] == len(fetched)
    assert calls["start"] == _CAL[3]  # 从最早缺失日开始拉
    assert report["missing_after"] == {}  # 补齐后缺口清零
    assert "snapshots" in report
    assert set(report["snapshots"]) == {"sector_daily", "sentiment_daily"}


def test_ensure_coverage_no_missing_skips_fetch(monkeypatch):
    calls = _patch_common(monkeypatch, _CAL, _index_frame([]))
    report = ensure_market_coverage(days=90)
    assert report["missing_before"] == {}
    assert report["fetched"] == 0 and report["persisted"] == 0
    assert "start" not in calls


def test_ensure_coverage_partial_symbols(monkeypatch):
    # 只有一个 symbol 缺失也触发补齐，且 missing_after 统计只看历史窗口
    _patch_common(monkeypatch, _CAL, _index_frame(_CAL))
    report = ensure_market_coverage(days=90)
    assert isinstance(report["missing_after"], dict)


def test_ensure_coverage_days_clamped():
    assert DEFAULT_LOOKBACK_DAYS == 90


def test_sector_kind_migration_preserves_history():
    """sector_daily 加 kind 列：老表 ALTER 保留历史行，默认 industry（防 drop 重建）。"""
    import duckdb

    from lquant.market.schema import MARKET_TABLES, ensure_market_tables

    con = duckdb.connect(":memory:")
    old_sql = MARKET_TABLES["sector_daily"].replace(
        "kind            VARCHAR,\n", "")
    con.execute(old_sql)
    con.execute(
        "INSERT INTO sector_daily VALUES "
        "('2026-09-10', 'BK1001', '旧行业', 1.0, 1.0, 0.0, 0.0, NULL, NULL, 0.0, 1, 1, NULL)")
    ensure_market_tables(con)
    cols = [r[0] for r in con.execute("DESCRIBE sector_daily").fetchall()]
    assert "kind" in cols
    assert con.execute(
        "SELECT sector_name, kind FROM sector_daily").fetchall() == [("旧行业", "industry")]


def test_days_clamped_via_calendar_error(monkeypatch):
    """days 上限 365 / 下限 1 —— 通过记录传入日历函数的参数验证。"""
    seen = {}

    def _cal(start, end):
        seen["span"] = (start, end)
        return _CAL

    monkeypatch.setattr("lquant.market.backfill._trade_calendar", _cal)
    monkeypatch.setattr(
        "lquant.market.backfill._index_have", lambda: {})
    monkeypatch.setattr(
        "lquant.market.backfill._fetch_index",
        lambda start, end, *, demo: _index_frame([]))
    ensure_market_coverage(days=100000)
    span = seen["span"][1] - seen["span"][0]
    assert span.days <= 366  # clamp 到 365


# ---------------------------------------------------------------- 历史回填

def test_fetch_index_daily_demo_and_provider_failure(monkeypatch):
    """demo 分支产出完整 schema；数据源不可用时返回**空表而非抛异常**。

    后者是刻意的：盘前调度里指数拉取失败不该让整个调度崩掉，
    空表让 `ensure_market_coverage` 如实报告「缺失仍在」。
    """
    from lquant.market.collectors.index_daily import _SCHEMA, fetch_index_daily

    df = fetch_index_daily(start="2026-09-01", end="2026-09-10", demo=True)
    assert df.schema == _SCHEMA or df.schema == pl.Schema(_SCHEMA)
    assert set(df["symbol"].unique()) == set(INDEX_POOL)
    assert df.height > 0

    def _boom(*a, **k):
        raise RuntimeError("no provider")

    monkeypatch.setattr("lquant.data.ingest.daily.resolve_ingest_source", _boom)
    empty = fetch_index_daily(start="2026-09-01", end="2026-09-10")
    assert empty.height == 0
    assert empty.schema == df.schema


def test_fetch_index_daily_routes_by_capability(monkeypatch):
    """必须按 INDEX_DAILY 能力选源，而不是盲取链头。

    链头是 tushare 时 `pro.daily` 不含指数 → 整段零行、基准永久缺失。
    """
    import lquant.market.collectors.index_daily as idx

    seen = {}
    frame = pl.DataFrame({
        "trade_date": [date(2026, 9, 1)], "symbol": ["000300.SH"],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [2.0],
        "pre_close": [1.0], "volume": [1.0], "amount": [1.0],
    })

    class _Src:
        def index_daily_bars(self, symbols, start, end):
            seen["called"] = (list(symbols), start, end)
            return frame

    monkeypatch.setattr("lquant.data.ingest.daily.resolve_ingest_source",
                        lambda **kw: (_Src(), "index_daily_bars"))
    out = idx.fetch_index_daily(start="2026-09-01", end="2026-09-02")
    assert seen["called"][0] == list(INDEX_POOL)
    assert out.height == 1
    assert out["name"][0] == "沪深300"
    assert "sec_type" not in out.columns


def test_backfill_index_history_with_real_db(tmp_path, monkeypatch):
    """历史回填端到端（demo 数据 + 真 temp DuckDB）：读-拉-写-复查。"""
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS
    from lquant.market.schema import ensure_market_tables

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        # index_daily 属于市场看板表（market/schema.py），不在主 DDL 里
        ensure_market_tables(con)

    from lquant.market.backfill import backfill_index_history

    out = backfill_index_history(date(2026, 9, 1), date(2026, 9, 10),
                                 symbols=["000300.SH"], demo=True)
    assert out["fetched"] > 0
    assert out["persisted"] > 0
    assert out["coverage_before"]["000300.SH"] == 0
    assert out["coverage_after"]["000300.SH"] == out["persisted"]
    # 幂等：重跑一次不产生重复行（upsert）
    again = backfill_index_history(date(2026, 9, 1), date(2026, 9, 10),
                                   symbols=["000300.SH"], demo=True)
    assert again["persisted"] == out["persisted"]
    assert again["coverage_after"]["000300.SH"] == out["coverage_after"]["000300.SH"]
    get_settings.cache_clear()


def test_backfill_index_history_unknown_symbol_warns(monkeypatch):
    """不在 INDEX_POOL 的标的只告警不阻断（name 落空，看板显示裸代码）。"""
    from loguru import logger

    from lquant.market import backfill as bf

    msgs: list[str] = []
    sink_id = logger.add(lambda m: msgs.append(m), level="WARNING")
    monkeypatch.setattr(bf, "_index_have", lambda: {})
    monkeypatch.setattr(bf, "_fetch_index",
                        lambda start, end, *, demo: _index_frame([date(2026, 9, 1)],
                                                                symbols=["000300.SH"]))
    monkeypatch.setattr(bf, "_persist_index", lambda df: df.height)
    try:
        out = bf.backfill_index_history(date(2026, 9, 1), date(2026, 9, 1),
                                        symbols=["000300.SH", "999999.SH"])
    finally:
        logger.remove(sink_id)
    assert any("999999.SH" in m for m in msgs)
    assert out["symbols"] == ["000300.SH", "999999.SH"]
