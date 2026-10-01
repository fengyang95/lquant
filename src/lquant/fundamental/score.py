"""基本面评分聚合：明细 → 单票总分 + 评级 + **覆盖率**。

**为什么必须暴露覆盖率**：FinancialTool 的分位缺失时走「无分位给 50% 基础分」，
静默给分，使用者无从判断这个总分是基于 17 个指标还是 2 个指标算出来的。
本实现改为：

- ``raw_score``：实际挣到的分数（缺指标就是少拿分）；
- ``normalized_score``：``raw_score / 已评分指标满分之和 × 100`` —— 跨覆盖度可比；
- ``coverage``：已评分指标占比，**必须和总分一起看**。

排序用 ``normalized_score``，但覆盖率低于 ``min_coverage`` 的票应当丢弃而不是
让它靠 2 个指标刷到满分 —— 是否丢弃交给调用方，本模块只如实报告。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import polars as pl

from lquant.fundamental.metrics import METRICS, MODULE_WEIGHTS, RatioMetric
from lquant.fundamental.percentile import (
    DEFAULT_MIN_SAMPLES,
    score_universe,
)

__all__ = [
    "DEFAULT_RATING_THRESHOLDS",
    "aggregate",
    "rating",
    "score_history",
    "score_snapshot",
]

DEFAULT_RATING_THRESHOLDS: tuple[float, float, float] = (85.0, 70.0, 60.0)
_RATING_LABELS = ("优秀", "良好", "一般", "较差")


def rating(normalized_score: float,
           thresholds: tuple[float, float, float] = DEFAULT_RATING_THRESHOLDS) -> str:
    """按归一化总分分档。"""
    good, fair, poor = thresholds
    if normalized_score >= good:
        return _RATING_LABELS[0]
    if normalized_score >= fair:
        return _RATING_LABELS[1]
    if normalized_score >= poor:
        return _RATING_LABELS[2]
    return _RATING_LABELS[3]


def aggregate(detail: pl.DataFrame,
              metrics: Sequence[RatioMetric] = METRICS,
              thresholds: tuple[float, float, float] = DEFAULT_RATING_THRESHOLDS
              ) -> pl.DataFrame:
    """明细长表 → 每票一行：模块分、总分、覆盖率、评级。

    Args:
        detail: :func:`~lquant.fundamental.percentile.score_universe` 的输出。
        metrics: 参与评分的指标全集（用于算覆盖率）。
        thresholds: 评级分档阈值。

    Returns:
        ``(symbol, industry, n_scored, n_metrics, coverage, raw_score,
        available_max, normalized_score, rating, score_<模块>…)``
    """
    cols = (["symbol", "industry", "n_scored", "n_metrics", "coverage", "raw_score",
             "available_max", "normalized_score", "rating"]
            + [f"score_{m}" for m in MODULE_WEIGHTS])
    if detail.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, "rating": pl.String})

    n_metrics = len(metrics)
    rows: list[dict] = []
    for (symbol, industry), grp in detail.group_by(["symbol", "industry"],
                                                   maintain_order=True):
        scored_modules = (grp.group_by("module")
                             .agg(pl.col("points").sum().alias("pts"),
                                  pl.col("max_score").sum().alias("mx")))
        points_by_mod = dict(zip(scored_modules["module"].to_list(),
                                 scored_modules["pts"].to_list(), strict=True))
        max_by_mod = dict(zip(scored_modules["module"].to_list(),
                              scored_modules["mx"].to_list(), strict=True))
        raw = float(sum(points_by_mod.values()))
        avail = float(sum(max_by_mod.values()))
        n_scored = grp.height
        row: dict = {
            "symbol": symbol,
            "industry": industry,
            "n_scored": n_scored,
            "n_metrics": n_metrics,
            "coverage": n_scored / n_metrics if n_metrics else 0.0,
            "raw_score": raw,
            "available_max": avail,
            "normalized_score": (raw / avail * 100.0) if avail > 0 else 0.0,
        }
        row["rating"] = rating(row["normalized_score"], thresholds)
        for mod in MODULE_WEIGHTS:
            row[f"score_{mod}"] = float(points_by_mod.get(mod, 0.0))
        rows.append(row)
    return pl.DataFrame(rows).select(cols).sort("symbol")


def score_snapshot(panel: pl.DataFrame, industry: pl.DataFrame, asof: date,
                   metrics: Sequence[RatioMetric] = METRICS,
                   min_samples: int = DEFAULT_MIN_SAMPLES,
                   thresholds: tuple[float, float, float] = DEFAULT_RATING_THRESHOLDS
                   ) -> pl.DataFrame:
    """单日快照：分位 → 明细 → 聚合，一步到位。"""
    detail = score_universe(panel, industry, asof, metrics, min_samples)
    return aggregate(detail, metrics, thresholds)


def score_history(panel: pl.DataFrame, industry: pl.DataFrame,
                  dates: Sequence[date],
                  metrics: Sequence[RatioMetric] = METRICS,
                  min_samples: int = DEFAULT_MIN_SAMPLES,
                  thresholds: tuple[float, float, float] = DEFAULT_RATING_THRESHOLDS
                  ) -> pl.DataFrame:
    """在多个观察日上滚动打分（**滚动历史分位**，对应 FinancialTool 的单日截面缺陷）。

    Returns:
        长表，多一列 ``asof_date``。
    """
    frames: list[pl.DataFrame] = []
    for d in dates:
        snap = score_snapshot(panel, industry, d, metrics, min_samples, thresholds)
        if snap.is_empty():
            continue
        frames.append(snap.with_columns(pl.lit(d).alias("asof_date")))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.String, "asof_date": pl.Date})
    out = pl.concat(frames, how="diagonal_relaxed")
    return out.select(["asof_date", *[c for c in out.columns if c != "asof_date"]]) \
              .sort(["asof_date", "symbol"])
