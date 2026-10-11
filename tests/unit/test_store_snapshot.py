"""``latest_snapshot``：日线湖 + 估值湖的横向快照。

为什么值得单测：两个湖的同步节奏不同（估值湖 ``daily_basic`` 落后日线是常态，
实测能落后两周以上），快照必须**各取自己最新的一天**。按「同一个日期」内连接
会让总市值整列变空 —— 那正是它要修的线上问题（个股页 成交额/总市值 显示「—」）。
全部走 tmp 湖（LQ_ROOT + cwd 隔离），不触共享数据湖。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.data.store import parquet as pq


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield pq
    get_settings.cache_clear()


def _daily(rows: list[tuple]) -> pl.DataFrame:
    """rows: (symbol, trade_date, close, pre_close, amount)"""
    return pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "trade_date": [r[1] for r in rows],
        "open": [float(r[2]) for r in rows],
        "high": [float(r[2]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[2]) for r in rows],
        "pre_close": [float(r[3]) for r in rows],
        "volume": [100.0] * len(rows),
        "amount": [float(r[4]) for r in rows],
    })


def _basic(rows: list[tuple]) -> pl.DataFrame:
    """rows: (symbol, trade_date, total_mv, float_mv, pe_ttm, turnover_rate)"""
    return pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "trade_date": [r[1] for r in rows],
        "total_mv": [None if r[2] is None else float(r[2]) for r in rows],
        "float_mv": [None if r[3] is None else float(r[3]) for r in rows],
        "pe_ttm": [None if r[4] is None else float(r[4]) for r in rows],
        "turnover_rate": [None if r[5] is None else float(r[5]) for r in rows],
    })


def test_empty_lake_returns_full_schema(lake) -> None:
    """空湖：0 行但列齐，调用方左连接/区间筛选不必先判列存在。"""
    snap = lake.latest_snapshot()
    assert snap.height == 0
    assert set(snap.columns) == set(pq.SNAPSHOT_SCHEMA)
    # 单只也一样
    assert lake.latest_snapshot(symbols=["600000.SH"]).height == 0


def test_latest_row_per_symbol_and_pct_backfill(lake) -> None:
    """每只取自己最新的一根；缺 pct_chg 时用 pre_close 反推（百分数口径）。"""
    lake.write_daily(_daily([
        ("600000.SH", date(2024, 1, 2), 10.0, 9.0, 1e8),
        ("600000.SH", date(2024, 1, 3), 11.0, 10.0, 2e8),
        ("000001.SZ", date(2024, 1, 2), 5.0, 5.0, 3e7),
    ]))
    snap = lake.latest_snapshot(asof=date(2024, 1, 3)).sort("symbol")

    assert snap["symbol"].to_list() == ["000001.SZ", "600000.SH"]
    by_sym = {r["symbol"]: r for r in snap.iter_rows(named=True)}
    row = by_sym["600000.SH"]
    assert row["trade_date"] == date(2024, 1, 3)
    assert row["close"] == 11.0 and row["pre_close"] == 10.0
    assert row["pct_chg"] == pytest.approx(10.0)
    # 湖里没有估值行 → 市值列存在但为空，不是整帧缺列
    assert row["total_mv"] is None and row["mv_date"] is None
    # 另一只的 pct_chg 为 0（pre_close == close）
    assert by_sym["000001.SZ"]["pct_chg"] == pytest.approx(0.0)


def test_existing_pct_chg_kept_and_only_null_backfilled(lake) -> None:
    """湖里已有 pct_chg 时以湖内值为准（含负号），只补那根为 null 的。"""
    df = _daily([
        ("600000.SH", date(2024, 1, 3), 11.0, 10.0, 2e8),
        ("000001.SZ", date(2024, 1, 3), 5.0, 4.0, 3e7),
    ]).with_columns(pl.Series("pct_chg", [9.8765, None]))
    lake.write_daily(df)

    by_sym = {r["symbol"]: r for r in
              lake.latest_snapshot(asof=date(2024, 1, 3)).iter_rows(named=True)}
    assert by_sym["600000.SH"]["pct_chg"] == pytest.approx(9.8765)   # 不被改写
    assert by_sym["000001.SZ"]["pct_chg"] == pytest.approx(25.0)     # null → 现算


def test_valuation_lake_may_lag_daily_lake(lake) -> None:
    """估值湖落后日线时，总市值取估值湖自己最新的一天（而不是跟着日线一起变空）。"""
    lake.write_daily(_daily([("600000.SH", date(2024, 3, 8), 12.0, 11.0, 1e8)]))
    lake.write_daily_basic(_basic([
        ("600000.SH", date(2024, 2, 20), 1.2e11, 1.0e11, 12.0, 1.5),
        ("600000.SH", date(2024, 2, 23), 1.3e11, 1.1e11, 13.0, 1.7),
    ]))
    snap = lake.latest_snapshot(asof=date(2024, 3, 8))
    row = snap.row(0, named=True)

    assert row["trade_date"] == date(2024, 3, 8)     # 价 / 额的口径日
    assert row["mv_date"] == date(2024, 2, 23)       # 市值的口径日（滞后）
    assert row["total_mv"] == pytest.approx(1.3e11)
    assert row["float_mv"] == pytest.approx(1.1e11)
    assert row["pe_ttm"] == pytest.approx(13.0)


def test_all_null_valuation_row_does_not_mask_previous(lake) -> None:
    """整行 NULL 的估值占位行不能当选「最新一行」——否则上一日的市值被抹成空。"""
    lake.write_daily(_daily([("600000.SH", date(2024, 3, 8), 12.0, 11.0, 1e8)]))
    lake.write_daily_basic(_basic([
        ("600000.SH", date(2024, 2, 23), 1.3e11, 1.1e11, 13.0, 1.7),
        ("600000.SH", date(2024, 3, 6), None, None, None, None),
    ]))
    row = lake.latest_snapshot(asof=date(2024, 3, 8)).row(0, named=True)
    assert row["mv_date"] == date(2024, 2, 23)
    assert row["total_mv"] == pytest.approx(1.3e11)


def test_symbols_filter_limits_rows(lake) -> None:
    lake.write_daily(_daily([
        ("600000.SH", date(2024, 1, 2), 10.0, 9.0, 1e8),
        ("000001.SZ", date(2024, 1, 2), 5.0, 5.0, 3e7),
    ]))
    snap = lake.latest_snapshot(symbols=["000001.SZ"])
    assert snap["symbol"].to_list() == ["000001.SZ"]


def test_asof_excludes_future_rows(lake) -> None:
    """asof 之后的数据不参与（PIT 安全：观察日不能看到未来价）。"""
    lake.write_daily(_daily([
        ("600000.SH", date(2024, 1, 2), 10.0, 9.0, 1e8),
        ("600000.SH", date(2024, 1, 9), 99.0, 10.0, 9e9),
    ]))
    row = lake.latest_snapshot(asof=date(2024, 1, 3)).row(0, named=True)
    assert row["trade_date"] == date(2024, 1, 2)
    assert row["close"] == 10.0
    # 字符串日期与 date 等价（API 层传的就是字符串）
    as_str = lake.latest_snapshot(asof="2024-01-03").row(0, named=True)
    assert as_str["trade_date"] == date(2024, 1, 2)


def test_valuation_only_symbol_still_appears(lake) -> None:
    """只有估值湖有行（还没有日线）时仍出现在快照里：价为空、市值有值。"""
    lake.write_daily_basic(_basic([
        ("600000.SH", date(2024, 2, 23), 1.3e11, 1.1e11, 13.0, 1.7),
    ]))
    snap = lake.latest_snapshot(asof=date(2024, 3, 8))
    assert snap.height == 1
    row = snap.row(0, named=True)
    assert row["close"] is None and row["amount"] is None
    assert row["trade_date"] is None
    assert row["total_mv"] == pytest.approx(1.3e11)
    assert row["mv_date"] == date(2024, 2, 23)
