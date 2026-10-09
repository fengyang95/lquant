"""摆动类指标：RSI（Wilder）/ KDJ。"""
from __future__ import annotations

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["add_rsi", "add_kdj"]


@register_indicator("rsi", label="RSI", category="oscillator", pane="sub", min_window=40,
                    outputs=("rsi14",))
def add_rsi(df: pl.DataFrame, n: int = 14, col: str = "close") -> pl.DataFrame:
    """Wilder RSI：涨跌幅的指数平滑（alpha=1/n）。首行无涨跌，置 null（不是 NaN）。"""
    chg = pl.col(col).diff()
    gain = pl.when(chg > 0).then(chg).otherwise(0.0)
    loss = pl.when(chg < 0).then(-chg).otherwise(0.0)
    df = df.with_columns(
        gain.ewm_mean(alpha=1 / n, adjust=False).alias("_g"),
        loss.ewm_mean(alpha=1 / n, adjust=False).alias("_l"),
    )
    # 首行 _g=_l=0 → 100*0/0=NaN；null 与 NaN 在 polars 是两回事，
    # drop_nulls 过滤不掉 NaN，必须在这里显式归 null。
    # 长期平盘（连续一字板/无波动的 vintage）同样 _g=_l=0 → 0/0，一并归 null：
    # RSI 此时无方向信息，输出 NaN 会顺着下游 zscore/rank 污染整列
    denom = pl.col("_g") + pl.col("_l")
    bad = (chg.is_null() | denom.is_null() | denom.is_nan() | (denom <= 0))
    df = df.with_columns(
        pl.when(bad).then(None)
        .otherwise(100 * pl.col("_g") / denom)
        .alias(f"rsi{n}")
    ).drop(["_g", "_l"])
    return df


@register_indicator("kdj", label="KDJ", category="oscillator", pane="sub", min_window=30,
                    inputs=("high", "low", "close"),
                    outputs=("kdj_k", "kdj_d", "kdj_j"))
def add_kdj(df: pl.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pl.DataFrame:
    """KDJ（国内口径）：RSV 取 n 日最高最低，K/D 用 alpha=1/m 的递推平滑，J = 3K − 2D。

    无波动的窗口（最高 = 最低）RSV 置 50，避免除零。
    """
    hhv = pl.col("high").rolling_max(n)
    llv = pl.col("low").rolling_min(n)
    span = hhv - llv
    rsv = (
        pl.when(span <= 0).then(50.0)
        .otherwise((pl.col("close") - llv) / span * 100)
    )
    df = df.with_columns(rsv.alias("_rsv"))
    df = df.with_columns(df["_rsv"].ewm_mean(alpha=1 / m1, adjust=False).alias("kdj_k"))
    df = df.with_columns(df["kdj_k"].ewm_mean(alpha=1 / m2, adjust=False).alias("kdj_d"))
    df = df.with_columns((3 * pl.col("kdj_k") - 2 * pl.col("kdj_d")).alias("kdj_j"))
    return df.drop("_rsv")
