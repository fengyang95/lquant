"""日线湖读取的两个回归点。

1. ``daily_range()`` 必须容忍**跨年 schema 漂移**。增量回填会改写单个年文件
   （例如某年补了 ``is_st``），跨年文件列集合就不再一致。原先它调
   ``pl.scan_parquet`` 时没带 ``extra_columns="ignore"``，会抛 SchemaError，
   再被下面的 ``except`` 吞掉 → 静默返回 ``(None, None)``。后果是
   ``latest_trade_date()`` 在**多年度真实湖**上永远返回 None，进而让
   ``latest_top_by_amount()``（新闻热点池）取不到任何标的。
   单年度测试湖没有漂移，所以这个 bug 长期没被现有用例发现。

2. ``read_daily_basic(symbols=...)`` 的标的过滤必须真的生效 —— 个股分析只要
   一只票的估值历史，全市场物化（数百万行）是纯粹的浪费。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """把 Parquet 湖根指到 tmp，返回 (lake_dir, module)。"""
    from lquant.core.config import get_settings

    monkeypatch.setenv("LQ_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from lquant.data.store import parquet as pq

    monkeypatch.setattr(pq, "_root", lambda: tmp_path / "parquet")
    yield tmp_path / "parquet", pq
    get_settings.cache_clear()


def _daily(df: pl.DataFrame, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def test_daily_range_survives_schema_drift(lake):
    """跨年列集合不一致时仍要读出真实区间（回归：静默返回 None）。"""
    root, pq = lake
    # 2024 年文件多一个 is_st 列（模拟增量回填后的漂移）
    _daily(pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)],
        "close": [10.0], "is_st": [False],
    }), root / "daily" / "year=2024" / "part-0.parquet")
    # 2025 年文件没有该列
    _daily(pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [date(2025, 6, 10)],
        "close": [11.0],
    }), root / "daily" / "year=2025" / "part-0.parquet")

    assert pq.daily_range() == (date(2024, 3, 1), date(2025, 6, 10))
    assert pq.latest_trade_date() == date(2025, 6, 10)


def test_latest_top_by_amount_survives_schema_drift(lake):
    """热点池依赖 latest_trade_date —— 漂移下也必须能选出标的。"""
    root, pq = lake
    _daily(pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)],
        "close": [10.0], "amount": [1e8], "is_st": [False],
    }), root / "daily" / "year=2024" / "part-0.parquet")
    _daily(pl.DataFrame({
        "symbol": ["600000.SH", "300750.SZ"],
        "trade_date": [date(2025, 6, 10), date(2025, 6, 10)],
        "close": [11.0, 200.0], "amount": [1e8, 5e8],
    }), root / "daily" / "year=2025" / "part-0.parquet")

    assert pq.latest_top_by_amount(1) == ["300750.SZ"]


def test_daily_range_empty_lake(lake):
    _, pq = lake
    assert pq.daily_range() == (None, None)
    assert pq.latest_trade_date() is None


def test_daily_range_degrades_on_read_error(lake, monkeypatch):
    """读湖失败依旧走降级路径（不能因为修了漂移就丢掉容错）。"""
    root, pq = lake
    _daily(pl.DataFrame({
        "symbol": ["600000.SH"], "trade_date": [date(2024, 3, 1)], "close": [10.0],
    }), root / "daily" / "year=2024" / "part-0.parquet")

    import polars as _pl

    def boom(*a, **k):
        raise RuntimeError("读湖失败")

    monkeypatch.setattr(_pl, "scan_parquet", boom)
    assert pq.daily_range() == (None, None)


def test_read_daily_basic_symbol_filter(lake):
    """symbols 过滤必须生效（个股分析只读自己那只票的历史）。"""
    root, pq = lake
    df = pl.DataFrame({
        "symbol": ["600519.SH", "600519.SH", "000001.SZ"],
        "trade_date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 2)],
        "close": [1700.0, 1710.0, 9.2],
        "pe_ttm": [30.0, 31.0, 5.0],
    })
    p = root / "daily_basic" / "year=2024" / "part-0.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(p)

    out = pq.read_daily_basic(symbols=["600519.SH"])
    assert out["symbol"].unique().to_list() == ["600519.SH"]
    assert out.height == 2

    # 不传 symbols 仍是全市场语义（向后兼容）
    assert pq.read_daily_basic().height == 3


def test_read_daily_basic_symbol_and_range_filter(lake):
    root, pq = lake
    df = pl.DataFrame({
        "symbol": ["600519.SH"] * 3,
        "trade_date": [date(2024, 1, 2), date(2024, 6, 3), date(2025, 1, 6)],
        "close": [1700.0, 1600.0, 1500.0],
        "pe_ttm": [30.0, 28.0, 25.0],
    })
    for year in (2024, 2025):
        sub = df.filter(pl.col("trade_date").dt.year() == year)
        p = root / "daily_basic" / f"year={year}" / "part-0.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        sub.write_parquet(p)

    out = pq.read_daily_basic("2024-02-01", "2024-12-31", symbols=["600519.SH"])
    assert out.height == 1
    assert out["trade_date"].to_list() == [date(2024, 6, 3)]


def test_read_daily_basic_empty_lake_returns_dataframe(lake):
    """空湖也必须返回 DataFrame（曾经返回 LazyFrame，只有空库才炸的坑）。"""
    _, pq = lake
    out = pq.read_daily_basic(symbols=["600519.SH"])
    assert isinstance(out, pl.DataFrame)
    assert out.is_empty()
    assert out.height == 0
    assert "symbol" in out.columns and "trade_date" in out.columns
