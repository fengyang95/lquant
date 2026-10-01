"""趋势类指标：MA / EMA / MACD / BOLL / BBI。

全部 Polars 表达式实现，无 talib 二进制依赖，逐列惰性可叠加。
所有指标在「历史不足」的行返回 null（不是 NaN）—— 调用方负责多取
``min_window`` 根再截尾。
"""
from __future__ import annotations

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["add_ma", "add_ema", "add_macd", "add_boll", "add_bbi"]


@register_indicator("ma", label="均线族", category="trend", min_window=60,
                    outputs=("ma5", "ma10", "ma20", "ma60"))
def add_ma(df: pl.DataFrame, periods: tuple[int, ...] = (5, 10, 20, 60),
           col: str = "close") -> pl.DataFrame:
    """移动平均。自定义 ``periods`` 时新增列名为 ``ma{p}``（元数据只登记默认档）。"""
    for n in periods:
        df = df.with_columns(pl.col(col).rolling_mean(n).alias(f"ma{n}"))
    return df


@register_indicator("ema", label="指数均线", category="trend", min_window=26,
                    outputs=("ema12", "ema26"))
def add_ema(df: pl.DataFrame, periods: tuple[int, ...] = (12, 26),
            col: str = "close") -> pl.DataFrame:
    """指数移动平均（span=n, adjust=False，与国内软件一致）。"""
    for n in periods:
        df = df.with_columns(pl.col(col).ewm_mean(span=n, adjust=False).alias(f"ema{n}"))
    return df


@register_indicator("macd", label="MACD", category="trend", min_window=60,
                    outputs=("ema12", "ema26", "macd_dif", "macd_dea", "macd_hist"))
def add_macd(df: pl.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9,
             col: str = "close", cn_hist: bool = True) -> pl.DataFrame:
    """MACD。``cn_hist=True`` 按国内软件口径 ``HIST = 2 × (DIF − DEA)``。"""
    df = add_ema(df, (fast, slow), col)
    df = df.with_columns((pl.col(f"ema{fast}") - pl.col(f"ema{slow}")).alias("macd_dif"))
    df = df.with_columns(
        pl.col("macd_dif").ewm_mean(span=signal, adjust=False).alias("macd_dea"))
    mult = 2 if cn_hist else 1
    df = df.with_columns((mult * (pl.col("macd_dif") - pl.col("macd_dea"))).alias("macd_hist"))
    return df


@register_indicator("boll", label="布林带", category="channel", min_window=40,
                    outputs=("boll_mid", "boll_upper", "boll_lower"))
def add_boll(df: pl.DataFrame, n: int = 20, k: float = 2.0,
             col: str = "close") -> pl.DataFrame:
    """布林带（中轨 = n 日均值，上下轨 = ±k 倍滚动标准差）。"""
    df = df.with_columns(pl.col(col).rolling_mean(n).alias("boll_mid"))
    std = pl.col(col).rolling_std(n)
    df = df.with_columns(
        (pl.col("boll_mid") + k * std).alias("boll_upper"),
        (pl.col("boll_mid") - k * std).alias("boll_lower"),
    )
    return df


@register_indicator("bbi", label="BBI 多空指标", category="trend", min_window=24,
                    outputs=("bbi",))
def add_bbi(df: pl.DataFrame, periods: tuple[int, ...] = (3, 6, 12, 24),
            col: str = "close") -> pl.DataFrame:
    """BBI = 四条均线的等权平均（多空分界）。"""
    mas = [pl.col(col).rolling_mean(n) for n in periods]
    return df.with_columns(sum(mas).truediv(len(mas)).alias("bbi"))
