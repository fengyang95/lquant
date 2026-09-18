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


@op("Ts_Quantile", "TS", 1, "时序分位数（q=0.8 时即 QTLU 口径）")
def ts_quantile(x: pl.Expr, n: int, q: float = 0.8) -> pl.Expr:
    return x.rolling_quantile(quantile=q, window_size=n).over("symbol")


@op("Ts_Slope", "TS", 1, "时序回归斜率")
def ts_slope(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float(np.polyfit(np.arange(len(s)), s, 1)[0]),
                         window_size=n).over("symbol")


@op("Ts_Rsquare", "TS", 1, "时序回归 R^2")
def ts_rsquare(x: pl.Expr, n: int) -> pl.Expr:
    def _r2(s):
        # rolling_map 回调拿到 polars Series：np.std 会分派到 Series.std(axis=…)
        # 直接 TypeError，必须先转 numpy
        if len(s) < 2 or np.std(s.to_numpy()) < 1e-12:
            return float("nan")
        r = np.corrcoef(np.arange(len(s)), s)[0, 1]
        return float(r * r)
    return x.rolling_map(lambda s: _r2(s), window_size=n).over("symbol")


@op("Ts_Resi", "TS", 1, "时序回归残差标准差")
def ts_resi(x: pl.Expr, n: int) -> pl.Expr:
    def _resi(s):
        if len(s) < 2:
            return float("nan")
        a = s.to_numpy()
        coef = np.polyfit(np.arange(len(a)), a, 1)
        fit = coef[0] * np.arange(len(a)) + coef[1]
        return float(np.sqrt(np.mean((a - fit) ** 2)))
    return x.rolling_map(lambda s: _resi(s), window_size=n).over("symbol")


@op("Ts_WMA", "TS", 1, "加权移动平均（权重 1..n）")
def ts_wma(x: pl.Expr, n: int) -> pl.Expr:
    def _wma(s):
        w = np.arange(1, len(s) + 1, dtype=float)
        return float(np.dot(w, s) / w.sum())
    return x.rolling_map(lambda s: _wma(s), window_size=n).over("symbol")
