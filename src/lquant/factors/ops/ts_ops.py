"""时序算子（TS）：在 symbol 分组内沿时间计算。"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.factors.ops.registry import op


@op("Ts_Mean", "TS", 1, "时序均值")
def ts_mean(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_mean(n).over("symbol")


@op("Ts_Std", "TS", 2, "时序标准差")
def ts_std(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_std(n).over("symbol")


@op("Ts_Return", "TS", 1, "N 期收益率")
def ts_return(x: pl.Expr, n: int) -> pl.Expr:
    return (x / x.shift(n).over("symbol") - 1).over("symbol")


@op("Ts_Delay", "TS", 1, "N 期前值")
def ts_delay(x: pl.Expr, n: int) -> pl.Expr:
    return x.shift(n).over("symbol")


@op("Ts_Corr", "TS", 2, "时序相关")
def ts_corr(x: pl.Expr, y: pl.Expr, n: int) -> pl.Expr:
    return pl.rolling_corr(x, y, window_size=n).over("symbol")


@op("Ts_Sum", "TS", 1, "时序求和")
def ts_sum(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_sum(n).over("symbol")


@op("Ts_Max", "TS", 1, "时序最大")
def ts_max(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_max(n).over("symbol")


@op("Ts_Min", "TS", 1, "时序最小")
def ts_min(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_min(n).over("symbol")


@op("Ts_ArgMax", "TS", 1, "时序最大值位置（0=窗内最老一根）")
def ts_argmax(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float(np.argmax(s)), window_size=n).over("symbol")


@op("Ts_ArgMin", "TS", 1, "时序最小值位置（0=窗内最老一根）")
def ts_argmin(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float(np.argmin(s)), window_size=n).over("symbol")


@op("Ts_Rank", "TS", 1, "时序秩（末值在窗口内的分位，0~1）")
def ts_rank(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float((s <= s[-1]).mean()), window_size=n).over("symbol")


@op("Ts_Delta", "TS", 1, "N 期差分")
def ts_delta(x: pl.Expr, n: int) -> pl.Expr:
    return (x - x.shift(n).over("symbol")).over("symbol")


@op("Ts_Cov", "TS", 2, "时序协方差")
def ts_cov(x: pl.Expr, y: pl.Expr, n: int) -> pl.Expr:
    return pl.rolling_cov(x, y, window_size=n).over("symbol")


@op("Ts_Skew", "TS", 1, "时序偏度")
def ts_skew(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_skew(n).over("symbol")


@op("Ts_Prod", "TS", 1, "时序连乘（log 域防溢出）")
def ts_prod(x: pl.Expr, n: int) -> pl.Expr:
    return (x.log1p().rolling_sum(n).over("symbol")).exp() - 1


@op("Ts_EMA", "TS", 1, "指数移动平均（span=n, adjust=False）")
def ts_ema(x: pl.Expr, n: int) -> pl.Expr:
    return x.ewm_mean(span=n, adjust=False).over("symbol")
