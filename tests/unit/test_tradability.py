"""可交易性打标：停牌/新股/ST。"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.quality.flags import ADJ_ANOMALY, NEW_LISTING, ST_RISK, SUSPENDED, ZOMBIE
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


def test_existing_flags_preserved_when_no_checks_apply():
    # 无 volume/listing_date/is_st 且无 dict 时：已有 quality_flags 位必须原样保留
    df = pl.DataFrame({
        "symbol": ["A"] * 4,
        "trade_date": [date(2020, 1, 2), date(2020, 1, 3),
                       date(2020, 1, 4), date(2020, 1, 5)],
        "close": [10.0] * 4,
        "quality_flags": [ADJ_ANOMALY, 0, ADJ_ANOMALY | ZOMBIE, 0],
    })
    out = flag_tradability(df)
    assert out["quality_flags"].to_list() == [ADJ_ANOMALY, 0,
                                              ADJ_ANOMALY | ZOMBIE, 0]


def test_listing_dates_dict_param():
    # 缺 listing_date 列时用 listing_dates dict 驱动
    df = _df()
    out = flag_tradability(df, listing_dates={"A": date(2020, 1, 1)})
    assert (out["quality_flags"] & NEW_LISTING != 0).all()
    # 上市满 120 天 → 不打标
    out2 = flag_tradability(df, listing_dates={"A": date(2019, 6, 1)})
    assert (out2["quality_flags"] & NEW_LISTING == 0).all()


def test_st_ranges_dict_param():
    # 缺 is_st 列时用 st_ranges dict 驱动：区间内打标、区间外不打
    df = _df()
    out = flag_tradability(
        df, st_ranges={"A": [(date(2020, 1, 3), date(2020, 1, 4))]})
    got = out["quality_flags"].to_list()
    assert got[1] & ST_RISK and got[2] & ST_RISK
    assert not (got[0] & ST_RISK) and not (got[3] & ST_RISK)
    # 区间与数据无交集 → 跳过检查
    out2 = flag_tradability(df, st_ranges={"A": [(date(2021, 1, 1), date(2021, 2, 1))]})
    assert (out2["quality_flags"] & ST_RISK == 0).all()
