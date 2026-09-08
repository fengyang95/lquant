"""截面算子（CS）：在同一 trade_date 上横截面计算。"""
from __future__ import annotations

import polars as pl

from lquant.factors.ops.registry import op


@op("Rank", "CS", 0, "截面排名")
def rank(x: pl.Expr) -> pl.Expr:
    return x.rank().over("trade_date")


@op("Scale", "CS", 0, "截面缩放（和=1）")
def scale(x: pl.Expr) -> pl.Expr:
    return (x / x.abs().sum().over("trade_date")).over("trade_date")


@op("Demean", "CS", 0, "截面去均值")
def demean(x: pl.Expr) -> pl.Expr:
    return (x - x.mean().over("trade_date")).over("trade_date")


@op("ZScore", "CS", 0, "截面标准化")
def zscore(x: pl.Expr) -> pl.Expr:
    return ((x - x.mean().over("trade_date")) / x.std().over("trade_date")).over("trade_date")
