"""可交易性打标：停牌/新股/ST。

这三类日子不是坏数据，而是「规则不同」：停牌日无成交价格不可信，
新股波动与涨跌停口径特殊，ST 涨跌幅限制只有 5%。直接删除会撕出
价格缺口，打标让下游按需过滤（如回测剔除或调整成本模型）。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.quality.flags import NEW_LISTING, ST_RISK, SUSPENDED, hit, or_flags

__all__ = ["flag_tradability"]

# 上市未满此天数视为新股（波动、流动性、涨跌停口径均特殊）
DEFAULT_MIN_LISTED_DAYS = 120


def _to_date(v: date | str) -> date:
    """str / date 统一转 python date（可安全参与 polars 日期运算）。"""
    return v if isinstance(v, date) else date.fromisoformat(v)


def _listing_expr(df: pl.DataFrame, listing_dates: dict | None,
                  min_listed_days: int) -> pl.Expr | None:
    """NEW_LISTING 判定：listing_date 列优先，缺列用 listing_dates（symbol → 上市日）。"""
    if "listing_date" in df.columns and "trade_date" in df.columns:
        listed = pl.col("listing_date").cast(pl.Date)
    elif listing_dates and "trade_date" in df.columns:
        # 未出现在 dict 里的 symbol 视为 null（跳过判定）
        days = [
            pl.when(pl.col("symbol") == sym)
            .then((pl.col("trade_date").cast(pl.Date) - _to_date(d))
                  .dt.total_days())
            for sym, d in listing_dates.items()
        ]
        listed_days = pl.coalesce(days)
        return hit(NEW_LISTING, listed_days.is_not_null()
                   & (listed_days < min_listed_days))
    else:
        return None
    listed_days = (pl.col("trade_date").cast(pl.Date) - listed).dt.total_days()
    return hit(NEW_LISTING, listed_days.is_not_null()
               & (listed_days < min_listed_days))


def _st_expr(df: pl.DataFrame, st_ranges: dict | None) -> pl.Expr | None:
    """ST_RISK 判定：is_st 列优先，缺列用 st_ranges（symbol → [(start, end), ...]）。"""
    if "is_st" in df.columns:
        return hit(ST_RISK, pl.col("is_st").fill_null(False))
    if not st_ranges or "trade_date" not in df.columns:
        return None
    # 逐 symbol 展开区间，行级判定：落在任一区间内即打标
    conds: list[pl.Expr] = []
    for sym, ranges in st_ranges.items():
        in_ranges = pl.lit(False)
        for start, end in ranges:
            in_ranges = in_ranges | pl.col("trade_date").cast(pl.Date) \
                .is_between(pl.lit(_to_date(start)), pl.lit(_to_date(end)))
        conds.append((pl.col("symbol") == sym) & in_ranges)
    if not conds:
        return None
    return hit(ST_RISK, pl.any_horizontal(conds))


def flag_tradability(df: pl.DataFrame, *, listing_dates: dict | None = None,
                     st_ranges: dict | None = None,
                     min_listed_days: int = DEFAULT_MIN_LISTED_DAYS) -> pl.DataFrame:
    """打标 SUSPENDED / NEW_LISTING / ST_RISK（缺输入时跳过对应检查）。

    - SUSPENDED：volume == 0（当日零成交）
    - NEW_LISTING：trade_date - listing_date < min_listed_days，
      优先用 df 的 listing_date 列，缺列用 listing_dates（symbol → 上市日）dict
    - ST_RISK：优先用 df 的 is_st 列（bool），缺列用 st_ranges
      （symbol → [(start, end), ...]）dict
    """
    flags: list[pl.Expr] = []
    if "volume" in df.columns:
        flags.append(hit(SUSPENDED, pl.col("volume") == 0))
    for expr in (_listing_expr(df, listing_dates, min_listed_days),
                 _st_expr(df, st_ranges)):
        if expr is not None:
            flags.append(expr)
    if not flags:
        # 已有 quality_flags（先前检查打的位）必须保留，不能覆盖成 0
        if "quality_flags" in df.columns:
            return df
        return df.with_columns(quality_flags=pl.lit(0, dtype=pl.Int32))
    return or_flags(df, *flags)
