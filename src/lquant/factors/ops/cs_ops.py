"""截面算子（CS）：在同一 trade_date 上横截面计算。

NaN 语义（2026-10-08 审计修复）：polars 的 mean/sum/std 对 NaN **不免疫**
—— 截面内一只股票的因子值是 NaN，整日截面的 mean/sum 都被染成 NaN；
零方差截面（全截面同值/长期停牌填充）则 std=0 → 0/0。此前这些算子对
两者都裸奔：一个 NaN 毒化整日全截面，零方差日整日 NaN 静默蒸发样本。
现统一：统计量只在**有限值**上计算（非有限输入行输出 null），零方差
分母兜底 1.0（输出与 preprocess.standardize._safe_std 同语义：记 0）。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.ops.registry import op


def _finite(x: pl.Expr) -> pl.Expr:
    """非有限值（NaN/±inf）→ null：统计量不再被毒化，该行输出 null。"""
    return pl.when(x.is_finite()).then(x).otherwise(None)


@op("Rank", "CS", 0, "截面排名")
def rank(x: pl.Expr) -> pl.Expr:
    # polars rank 把 NaN 排在所有有限值之上：不过滤的话「因子无效」的
    # 股票会被当成「因子最强」参与选股/分组
    return _finite(x).rank().over("trade_date")


@op("Scale", "CS", 0, "截面缩放（和=1）")
def scale(x: pl.Expr) -> pl.Expr:
    v = _finite(x)
    denom = pl.when(v.abs().sum().over("trade_date") > 1e-12) \
              .then(v.abs().sum().over("trade_date")).otherwise(1.0)
    return (v / denom).over("trade_date")


@op("Demean", "CS", 0, "截面去均值")
def demean(x: pl.Expr) -> pl.Expr:
    v = _finite(x)
    return (v - v.mean().over("trade_date")).over("trade_date")


@op("ZScore", "CS", 0, "截面标准化")
def zscore(x: pl.Expr) -> pl.Expr:
    v = _finite(x)
    std = v.std().over("trade_date")
    # 零方差截面 σ≤1e-12 兜底 1.0 → (x-mean)/1 = 0，与
    # preprocess.standardize._safe_std 同语义（记 0 不记 null）
    safe_std = pl.when(std > 1e-12).then(std).otherwise(1.0)
    return ((v - v.mean().over("trade_date")) / safe_std).over("trade_date")
