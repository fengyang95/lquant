"""可交易性打标：停牌/新股/ST。

这三类日子不是坏数据，而是「规则不同」：停牌日无成交价格不可信，
新股波动与涨跌停口径特殊，ST 涨跌幅限制只有 5%。直接删除会撕出
价格缺口，打标让下游按需过滤（如回测剔除或调整成本模型）。
"""
from __future__ import annotations

import polars as pl

from lquant.data.quality.flags import NEW_LISTING, ST_RISK, SUSPENDED, hit, or_flags

__all__ = ["flag_tradability"]

# 上市未满此天数视为新股（波动、流动性、涨跌停口径均特殊）
DEFAULT_MIN_LISTED_DAYS = 120


def flag_tradability(df: pl.DataFrame, *,
                     min_listed_days: int = DEFAULT_MIN_LISTED_DAYS) -> pl.DataFrame:
    """打标 SUSPENDED / NEW_LISTING / ST_RISK（缺输入列时跳过对应检查）。

    - SUSPENDED：volume == 0（当日零成交）
    - NEW_LISTING：trade_date - listing_date < min_listed_days
      （df 需带 listing_date 列，日期口径不限，统一转 Date 后相减）
    - ST_RISK：df 需带 is_st 列（bool）
    """
    flags: list[pl.Expr] = []
    if "volume" in df.columns:
        flags.append(hit(SUSPENDED, pl.col("volume") == 0))
    if "listing_date" in df.columns and "trade_date" in df.columns:
        listed_days = (pl.col("trade_date").cast(pl.Date)
                       - pl.col("listing_date").cast(pl.Date))
        flags.append(hit(NEW_LISTING,
                         listed_days.dt.total_days() < min_listed_days))
    if "is_st" in df.columns:
        flags.append(hit(ST_RISK, pl.col("is_st").fill_null(False)))
    if not flags:
        return df.with_columns(quality_flags=pl.lit(0, dtype=pl.Int32))
    return or_flags(df, *flags)
