"""可交易性打标：停牌/新股/ST。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.quality.flags import NEW_LISTING, ST_RISK, SUSPENDED
from lquant.data.quality.tradability import flag_tradability


def _df():
    # trade_date 用真实日期：NEW_LISTING 检查需要 trade_date - listing_date
    return pl.DataFrame({
        "symbol": ["A"] * 4,
        "trade_date": [date(2020, 1, 2), date(2020, 1, 3),
                       date(2020, 1, 4), date(2020, 1, 5)],
        "close": [10.0] * 4,
        "volume": [100.0, 0.0, 100.0, 100.0],
    })


def test_suspension_flagged():
    out = flag_tradability(_df())
    hit_row = out.filter(pl.col("trade_date") == date(2020, 1, 3))
    assert (hit_row["quality_flags"].item() & SUSPENDED) != 0


def test_new_listing():
    df = _df().with_columns(pl.lit(date(2020, 1, 1)).alias("listing_date"))
    # 距 listing_date 均不足 120 天 → 全部打 NEW_LISTING
    out = flag_tradability(df)
    assert (out["quality_flags"] & NEW_LISTING != 0).all()


def test_new_listing_expired():
    # 上市满 120 天后不再打标
    df = _df().with_columns(pl.lit(date(2019, 6, 1)).alias("listing_date"))
    out = flag_tradability(df)
    assert (out["quality_flags"] & NEW_LISTING == 0).all()


def test_st_flagged():
    df = _df().with_columns(pl.lit(True).alias("is_st"))
    out = flag_tradability(df)
    assert (out["quality_flags"] & ST_RISK != 0).all()


def test_no_columns_no_crash():
    # 缺 listing_date / is_st 列时跳过对应检查，SUSPENDED 仍生效
    out = flag_tradability(_df())
    assert (out["quality_flags"] & NEW_LISTING == 0).all()
    assert (out["quality_flags"] & ST_RISK == 0).all()
    assert (out["quality_flags"] & SUSPENDED != 0).any()
