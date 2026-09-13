"""daily_basic 合并逻辑测试：coalesce 纯函数只填 NULL、不覆盖主源。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.ingest.daily_basic import coalesce_daily_basic
from lquant.data.schema import SCHEMAS


def _daily_frame() -> pl.DataFrame:
    cols = {k: [] for k in SCHEMAS["daily_bar"]}
    df = pl.DataFrame(cols)
    return pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
        "trade_date": [date(2026, 8, 27)] * 3,
        "close": [10.0, 10.0, 12.0],
        # 茅台已有 pe_ttm（主源 baostock）：merge 不得覆盖
        "pe_ttm": [19.9, 19.9, None],
        "pb_mrq": [None, None, None],
        "ps_ttm": [None, None, None],
        "total_mv": [None, None, None],
        "float_mv": [None, None, None],
    })


def _basic_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "trade_date": [date(2026, 8, 27)] * 2,
        "close": [10.0, 12.0],
        "turnover_rate": [0.5, 0.9],
        "pe_ttm": [5.5, 6.6],
        "pb_mrq": [0.6, 0.7],
        "ps_ttm": [1.1, 1.2],
        "total_mv": [2.0e10, 3.0e10],
        "float_mv": [1.5e10, 2.5e10],
        "dv_ttm": [3.0, 4.0],
        "total_share": [3.0e9, 4.0e9],
        "float_share": [2.5e9, 3.5e9],
        "source": ["tushare", "tushare"],
    })


def test_coalesce_fills_only_nulls() -> None:
    out, filled = coalesce_daily_basic(_daily_frame(), _basic_frame())
    # 茅台 pe_ttm 已有值 → 不被 tushare 覆盖
    mt = out.filter(pl.col("symbol") == "600000.SH")
    assert mt["pe_ttm"][0] == 19.9
    assert mt["total_mv"][0] == 2.0e10      # 全 NULL 列被填上
    assert mt["pb_mrq"][0] == 0.6
    # 平安 pe_ttm 是 NULL → 被填
    pa = out.filter(pl.col("symbol") == "000001.SZ")
    assert pa["pe_ttm"][0] == 6.6
    # 同一行内：600000.SH 有 2 行，两行各填各的（join 不会错位）
    assert filled == {
        "pe_ttm": 1, "pb_mrq": 3, "ps_ttm": 3, "total_mv": 3, "float_mv": 3,
    }
    # basic 辅助列不残留
    assert not any(c.startswith("__") for c in out.columns)


def test_coalesce_empty_basic_noop() -> None:
    df = _daily_frame()
    out, filled = coalesce_daily_basic(df, SCHEMAS and pl.DataFrame(
        schema={k: v for k, v in SCHEMAS["daily_basic"].items()}
    ))
    assert out.equals(df)
    assert all(v == 0 for v in filled.values())


def test_coalesce_old_daily_missing_column() -> None:
    """老年文件缺 pe_ttm 列 → 先补空列再 join，不炸；3 行全部被填。"""
    df = _daily_frame().drop("pe_ttm")
    out, filled = coalesce_daily_basic(df, _basic_frame())
    assert "pe_ttm" in out.columns
    assert filled["pe_ttm"] == 3
