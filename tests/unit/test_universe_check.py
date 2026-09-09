"""PIT 股票池检查：抓幸存者偏差。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.quality.universe import check_point_in_time


def test_shrinking_universe_flags_risk():
    # 前 10 天 100 只，后 10 天只剩 50 只 → 骤降 → 幸存者偏差风险
    rows = [{"symbol": f"S{i:03d}", "trade_date": d}
            for d in range(1, 11) for i in range(100)]
    rows += [{"symbol": f"S{i:03d}", "trade_date": d}
             for d in range(11, 21) for i in range(50)]
    df = pl.DataFrame(rows)
    r = check_point_in_time(df)
    assert r["survivorship_risk"] is True
    assert r["reasons"]


def test_growing_universe_ok():
    rows = [{"symbol": f"S{i:03d}", "trade_date": d}
            for d in range(1, 11) for i in range(50)]
    rows += [{"symbol": f"S{i:03d}", "trade_date": d}
             for d in range(11, 21) for i in range(100)]
    df = pl.DataFrame(rows)
    r = check_point_in_time(df)
    assert r["survivorship_risk"] is False
    assert r["daily_counts"].height == 20


def test_delist_date_column_checked():
    # 带 delist_date：退市日之后仍出现的行 → 幸存者偏差线索
    rows = [{"symbol": "A", "trade_date": date(2020, 1, d)} for d in range(1, 11)]
    rows += [{"symbol": "B", "trade_date": date(2020, 1, d)} for d in range(1, 6)]
    df = pl.DataFrame(rows).with_columns(
        pl.when(pl.col("symbol") == "B")
        .then(pl.lit(date(2020, 1, 3)))
        .otherwise(pl.lit(None, dtype=pl.Date))
        .alias("delist_date"),
    )
    r = check_point_in_time(df)
    assert r["survivorship_risk"] is True
    assert any("delist" in x for x in r["reasons"])


def test_custom_col_names():
    df = pl.DataFrame({"code": ["A", "B"], "dt": [1, 1]})
    r = check_point_in_time(df, date_col="dt", symbol_col="code")
    assert r["daily_counts"].height == 1
