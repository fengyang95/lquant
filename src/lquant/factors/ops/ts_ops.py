"""时序算子（TS）：在 symbol 分组内沿时间计算。"""
from __future__ import annotations

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
