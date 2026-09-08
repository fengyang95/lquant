"""标准化：ZScore / MinMax / Rank。

去极值之后必须做，否则不同量纲的因子没法加权合成。
Rank 是 A 股实操里最常用的 —— 它同时完成了标准化和非线性压缩。
"""
from __future__ import annotations

import math

import polars as pl

from lquant.factors.preprocess.registry import method


def _inv_norm(p: float) -> float:
    """Acklam 逆正态近似，精度 ~1e-9。用它避免为单个函数引 scipy 依赖。"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _safe_std(std: pl.Expr) -> pl.Expr:
    return pl.when(std > 1e-12).then(std).otherwise(pl.lit(1.0))


@method("zscore", stage="standardize", label="Z-Score")
def zscore(df: pl.DataFrame, col: str, *, by: str = "trade_date") -> pl.DataFrame:
    """截面均值 0、标准差 1。"""
    mean = pl.col(col).mean().over(by)
    std = _safe_std(pl.col(col).std().over(by))
    return df.with_columns(((pl.col(col) - mean) / std).alias(col))


@method("minmax", stage="standardize", label="Min-Max")
def minmax(df: pl.DataFrame, col: str, *, by: str = "trade_date",
           lo: float = 0.0, hi: float = 1.0) -> pl.DataFrame:
    """线性映射到 [lo, hi]。对极值不如 Rank 稳健。"""
    mn = pl.col(col).min().over(by)
    span = pl.when(pl.col(col).max().over(by) - mn > 1e-12) \
              .then(pl.col(col).max().over(by) - mn).otherwise(pl.lit(1.0))
    return df.with_columns((lo + (pl.col(col) - mn) / span * (hi - lo)).alias(col))


@method("rank", stage="standardize", label="截面排名", params={"to": "uniform"})
def rank(df: pl.DataFrame, col: str, *, by: str = "trade_date",
         to: str = "uniform") -> pl.DataFrame:
    """截面排名归一化。to=uniform → [0,1]；to=normal → 近似标准正态。

    normal 用逆正态变换，多因子合成时比 uniform 更好（正态假设下可直接相加）。
    """
    n = pl.col(col).count().over(by)
    pct = (pl.col(col).rank("average").over(by) / n).clip(0.5 / n, 1 - 0.5 / n)
    if to == "normal":
        pct = pct.map_elements(_inv_norm, return_dtype=pl.Float64)
    return df.with_columns(pct.alias(col))


@method("none", stage="standardize", label="不标准化")
def none(df: pl.DataFrame, col: str, *, by: str = "trade_date") -> pl.DataFrame:
    return df
