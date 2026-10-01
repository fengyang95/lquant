"""量能类指标：量比 / 量能突增 / 换手均线。"""
from __future__ import annotations

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["add_volume_ratio", "add_volume_surge", "add_turnover_ma"]


@register_indicator("volume_ratio", label="量比", category="volume", min_window=20,
                    inputs=("volume",), outputs=("volume_ratio",))
def add_volume_ratio(df: pl.DataFrame, n: int = 20, col: str = "volume") -> pl.DataFrame:
    """量比 = 当日量 / 过去 ``n`` 日均量。

    均量窗口**不含当日**（``shift(1)`` 后再滚动），否则放量当天会把分母一起抬高、
    自己稀释掉信号 —— 这是量比类形态最常被写错的地方。
    """
    base = pl.col(col).shift(1).rolling_mean(n)
    out = pl.when(base <= 0).then(None).otherwise(pl.col(col) / base)
    return df.with_columns(out.alias("volume_ratio"))


@register_indicator("volume_surge", label="量能突增", category="volume", min_window=25,
                    inputs=("volume",), outputs=("volume_surge",))
def add_volume_surge(df: pl.DataFrame, n: int = 5, ratio: float = 1.5,
                     col: str = "volume") -> pl.DataFrame:
    """量能突增标记：当日量 > ``ratio`` × 过去 n 日均量（窗口同样不含当日）。"""
    base = pl.col(col).shift(1).rolling_mean(n)
    flag = (base > 0) & (pl.col(col) > ratio * base)
    return df.with_columns(flag.fill_null(False).alias("volume_surge"))


@register_indicator("turnover_ma", label="换手率均线", category="volume", min_window=5,
                    inputs=("turnover_rate",), outputs=("turnover_ma5",))
def add_turnover_ma(df: pl.DataFrame, n: int = 5,
                    col: str = "turnover_rate") -> pl.DataFrame:
    """换手率 n 日均值（缺列时原样返回，便于异构数据源共用同一调用链）。"""
    if col not in df.columns:
        return df
    return df.with_columns(pl.col(col).rolling_mean(n).alias(f"turnover_ma{n}"))
