"""复合指标：把常用指标一次性叠加（个股详情页 / 快速分析用）。"""
from __future__ import annotations

import polars as pl

from lquant.indicators.momentum import add_rsi
from lquant.indicators.trend import add_boll, add_ma, add_macd

__all__ = ["add_all"]

#: 各指标的预热根数（取数时至少多取这么多根，否则头部为 null）
WARMUP: dict[str, int] = {"ma": 60, "macd": 60, "rsi": 40, "boll": 40}


def add_all(df: pl.DataFrame, col: str = "close") -> pl.DataFrame:
    """一次叠加全部常用指标（MA / MACD / RSI / BOLL）。"""
    df = add_ma(df, col=col)
    df = add_macd(df, col=col)
    df = add_rsi(df, col=col)
    return add_boll(df, col=col)
