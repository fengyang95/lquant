"""去极值：MAD / 分位数 / 3σ / 硬截断。

A 股原始因子的极值比美股严重得多 —— 一字板、ST、次新都能把均值拉飞。
直接算 IC 的话，IC 基本被这几只票绑架，是典型的假信号。

统一签名：fn(df, col, *, by="trade_date", **kw) -> pl.DataFrame
所有方法按 `by` 分组做**截面**处理，绝不跨日期污染（否则就是未来函数）。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.preprocess.registry import method

_MAD_K = 1.4826  # MAD → 标准差的一致性修正（正态分布下）


def _safe_scale(scale: pl.Expr, fallback: pl.Expr) -> pl.Expr:
    """离散度为 0 时（如全市场同一个值）退化处理，避免除零产生 inf。"""
    return pl.when(scale > 1e-12).then(scale).otherwise(fallback)


@method("mad", stage="winsorize", label="MAD 去极值", params={"n": 5.0})
def mad(df: pl.DataFrame, col: str, *, by: str = "trade_date", n: float = 5.0) -> pl.DataFrame:
    """中位数 ± n × 1.4826 × MAD。

    比 3σ 稳健：σ 本身会被极值撑大，MAD 不会。默认 n=5（业界常用 3~5）。
    """
    med = pl.col(col).median().over(by)
    dev = (pl.col(col) - med).abs().median().over(by) * _MAD_K
    std = pl.col(col).std().over(by)
    scale = _safe_scale(dev, std)
    return df.with_columns(pl.col(col).clip(med - n * scale, med + n * scale))


@method("quantile", stage="winsorize", label="分位数截断", params={"q": 0.01})
def quantile(df: pl.DataFrame, col: str, *, by: str = "trade_date", q: float = 0.01) -> pl.DataFrame:
    """按分位数双侧截断，q=0.01 即 1%/99%。"""
    lo = pl.col(col).quantile(q).over(by)
    hi = pl.col(col).quantile(1 - q).over(by)
    return df.with_columns(pl.col(col).clip(lo, hi))


@method("three_sigma", stage="winsorize", label="3σ 去极值", params={"n": 3.0})
def three_sigma(df: pl.DataFrame, col: str, *, by: str = "trade_date", n: float = 3.0) -> pl.DataFrame:
    """均值 ± n × σ。对正态分布有效，重尾分布下不如 MAD。"""
    mean = pl.col(col).mean().over(by)
    std = _safe_scale(pl.col(col).std().over(by), pl.lit(1.0))
    return df.with_columns(pl.col(col).clip(mean - n * std, mean + n * std))


@method("clip", stage="winsorize", label="硬截断", params={"lo": -10.0, "hi": 10.0})
def clip(df: pl.DataFrame, col: str, *, by: str = "trade_date",
         lo: float = -10.0, hi: float = 10.0) -> pl.DataFrame:
    """固定上下界。适合已经标准化过、只需兜底的场景。"""
    return df.with_columns(pl.col(col).clip(lo, hi))


@method("none", stage="winsorize", label="不去极值")
def none(df: pl.DataFrame, col: str, *, by: str = "trade_date") -> pl.DataFrame:
    """透传。用于对比实验：确认去极值到底贡献了多少。"""
    return df
