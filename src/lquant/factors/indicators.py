"""技术指标（方案 M3，P0）—— 个股详情页叠加用。

全部 Polars 表达式实现，输入是带 close 列的 DataFrame：
- MA / EMA（均线族）
- MACD(12,26,9)（DIF/DEA/HIST，国内口径 HIST = 2×(DIF-DEA)）
- RSI(14)（Wilder 平滑）
- BOLL(20,2)（上中下轨）

设计约束：不引入 talib（二进制依赖重），且逐列惰性可叠加。
所有指标在「有足够历史」的行上才有值，warmup 期返回 null ——
调用方负责取数时多取 history bars 再 tail。
"""
from __future__ import annotations

import polars as pl

__all__ = ["add_ma", "add_ema", "add_macd", "add_rsi", "add_boll", "add_all"]

# 各指标的 warmup bars（取数时至少多取这么多根）
WARMUP: dict[str, int] = {"ma": 60, "macd": 60, "rsi": 40, "boll": 40}


def add_ma(df: pl.DataFrame, periods: tuple[int, ...] = (5, 10, 20, 60),
           col: str = "close") -> pl.DataFrame:
    for n in periods:
        df = df.with_columns(pl.col(col).rolling_mean(n).alias(f"ma{n}"))
    return df


def add_ema(df: pl.DataFrame, periods: tuple[int, ...] = (12, 26),
            col: str = "close") -> pl.DataFrame:
    for n in periods:
        df = df.with_columns(
            pl.col(col).ewm_mean(span=n, adjust=False).alias(f"ema{n}"))
    return df


def add_macd(df: pl.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9,
             col: str = "close", cn_hist: bool = True) -> pl.DataFrame:
    """MACD。cn_hist=True 按国内软件口径 HIST=2*(DIF-DEA)。"""
    df = add_ema(df, (fast, slow), col)
    df = df.with_columns((pl.col(f"ema{fast}") - pl.col(f"ema{slow}")).alias("macd_dif"))
    df = df.with_columns(
        pl.col("macd_dif").ewm_mean(span=signal, adjust=False).alias("macd_dea"))
    mult = 2 if cn_hist else 1
    df = df.with_columns((mult * (pl.col("macd_dif") - pl.col("macd_dea"))).alias("macd_hist"))
    return df


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
    # drop_nulls 过滤不掉 NaN，必须在这里显式归 null
    df = df.with_columns(
        pl.when(chg.is_null()).then(None)
        .otherwise(100 * pl.col("_g") / (pl.col("_g") + pl.col("_l")))
        .alias(f"rsi{n}")
    ).drop(["_g", "_l"])
    return df


def add_boll(df: pl.DataFrame, n: int = 20, k: float = 2.0,
             col: str = "close") -> pl.DataFrame:
    df = df.with_columns(pl.col(col).rolling_mean(n).alias("boll_mid"))
    df = df.with_columns(
        (pl.col("boll_mid") + k * pl.col(col).rolling_std(n)).alias("boll_upper"),
        (pl.col("boll_mid") - k * pl.col(col).rolling_std(n)).alias("boll_lower"),
    )
    return df


def add_all(df: pl.DataFrame, col: str = "close") -> pl.DataFrame:
    """一次叠加全部常用指标（个股详情页用）。"""
    df = add_ma(df, col=col)
    df = add_macd(df, col=col)
    df = add_rsi(df, col=col)
    df = add_boll(df, col=col)
    return df
