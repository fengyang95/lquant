"""行业相对分位：把绝对阈值换成「同行业内的相对位置」。

**为什么必须是这样**：毛利率 60% 对白酒是常态，对商贸零售是异常；
存货周转 200 天对地产正常，对生鲜零售是灾难。跨行业比绝对阈值没有意义。

**与 FinancialTool 的差异**：那个实现自称基于「过去 5 年历史分布」，
实际只取**单一报告日的横截面**且日期硬编码（``date="20251231"``），
既不滚动也不做公告日对齐。本实现：

1. 每个观察日的分位都**只由 ``pub_date <= asof`` 的数据算出**（PIT）；
2. 观察日由调用方传入 —— 在调仓日循环调用即得到滚动分位，
   而不是一次性钉死某一天的截面；
3. 样本不足 ``min_samples`` 时**不出分位**（返回 null），
   而不是用默认值悄悄给分。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from lquant.fundamental.metrics import METRICS, RatioMetric
from lquant.fundamental.panel import resolve_industry, resolve_pit

__all__ = [
    "DEFAULT_MIN_SAMPLES",
    "PercentileBand",
    "percentile_bands",
    "percentile_table",
    "score_by_percentile",
    "score_universe",
]

DEFAULT_MIN_SAMPLES = 5

#: 分位档位系数：≥P75 → 1.0，≥P50 → 0.8，≥P25 → 0.5，其余 0.2（反向指标对称）
_BELOW_FLOOR = 0.2


@dataclass(frozen=True)
class PercentileBand:
    """某行业某指标的横截面分位。"""

    p25: float
    p50: float
    p75: float
    n: int


def percentile_bands(values: Sequence[float],
                     min_samples: int = DEFAULT_MIN_SAMPLES) -> PercentileBand | None:
    """计算 P25/P50/P75；样本不足返回 ``None``（**不用默认值兜底**）。"""
    xs = [float(v) for v in values if v is not None]
    if len(xs) < min_samples:
        return None
    s = pl.Series("v", xs)
    q = s.quantile([0.25, 0.5, 0.75], interpolation="linear")
    return PercentileBand(float(q[0]), float(q[1]), float(q[2]), len(xs))


def score_by_percentile(value: float, band: PercentileBand,
                        *, higher_better: bool = True) -> float:
    """按分位给系数（0.2 / 0.5 / 0.8 / 1.0）。

    ``higher_better=True``：值 ≥ P75 满分（同类中的头部）。
    ``higher_better=False``（周转天数、PE、资产负债率等）：值 ≤ P25 满分。
    """
    v = float(value)
    if higher_better:
        if v >= band.p75:
            return 1.0
        if v >= band.p50:
            return 0.8
        if v >= band.p25:
            return 0.5
        return _BELOW_FLOOR
    if v <= band.p25:
        return 1.0
    if v <= band.p50:
        return 0.8
    if v <= band.p75:
        return 0.5
    return _BELOW_FLOOR


def percentile_table(panel: pl.DataFrame, industry: pl.DataFrame, asof: date,
                     metrics: Sequence[RatioMetric] = METRICS,
                     min_samples: int = DEFAULT_MIN_SAMPLES) -> pl.DataFrame:
    """在 ``asof`` 日，逐 (行业, 指标) 计算分位。

    Returns:
        长表 ``(industry, item, p25, p50, p75, n)``；样本不足的组合不出现。
    """
    items = [m.item for m in metrics]
    wide = resolve_pit(panel, asof, items=items)
    ind = resolve_industry(industry, asof)
    schema = {"industry": pl.String, "item": pl.String, "p25": pl.Float64,
              "p50": pl.Float64, "p75": pl.Float64, "n": pl.Int64}
    if wide.is_empty() or ind.is_empty():
        return pl.DataFrame(schema=schema)

    merged = wide.join(ind, on="symbol", how="inner")
    present = [c for c in items if c in merged.columns]
    if not present:
        return pl.DataFrame(schema=schema)

    long = merged.select(["symbol", "industry", *present]).unpivot(
        on=present, index=["symbol", "industry"],
        variable_name="item", value_name="value").drop_nulls("value")

    rows: list[dict] = []
    for (ind_name, item), grp in long.group_by(["industry", "item"], maintain_order=True):
        band = percentile_bands(grp["value"].to_list(), min_samples)
        if band is None:
            continue
        rows.append({"industry": ind_name, "item": item, "p25": band.p25,
                     "p50": band.p50, "p75": band.p75, "n": band.n})
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def score_universe(panel: pl.DataFrame, industry: pl.DataFrame, asof: date,
                   metrics: Sequence[RatioMetric] = METRICS,
                   min_samples: int = DEFAULT_MIN_SAMPLES) -> pl.DataFrame:
    """在 ``asof`` 日给全市场打分（明细长表）。

    Returns:
        ``(symbol, industry, item, label, module, value, p25, p50, p75, n,
        ratio, points, max_score)``。只包含「有数据且所属行业分位可用」的行；
        缺失情况由调用方按覆盖率统计（见 :func:`lquant.fundamental.score.aggregate`）。
    """
    cols = ["symbol", "industry", "item", "label", "module", "value",
            "p25", "p50", "p75", "n", "ratio", "points", "max_score"]
    schema = {"symbol": pl.String, "industry": pl.String, "item": pl.String,
              "label": pl.String, "module": pl.String, "value": pl.Float64,
              "p25": pl.Float64, "p50": pl.Float64, "p75": pl.Float64,
              "n": pl.Int64, "ratio": pl.Float64, "points": pl.Float64,
              "max_score": pl.Float64}

    band_df = percentile_table(panel, industry, asof, metrics, min_samples)
    if band_df.is_empty():
        return pl.DataFrame(schema=schema)

    items = [m.item for m in metrics]
    wide = resolve_pit(panel, asof, items=items)
    ind = resolve_industry(industry, asof)
    if wide.is_empty() or ind.is_empty():
        return pl.DataFrame(schema=schema)
    merged = wide.join(ind, on="symbol", how="inner")
    present = [c for c in items if c in merged.columns]
    long = merged.select(["symbol", "industry", *present]).unpivot(
        on=present, index=["symbol", "industry"],
        variable_name="item", value_name="value").drop_nulls("value")

    joined = long.join(band_df, on=["industry", "item"], how="inner")
    by_item = {m.item: m for m in metrics}
    rows: list[dict] = []
    for r in joined.iter_rows(named=True):
        meta = by_item.get(r["item"])
        if meta is None:
            continue
        band = PercentileBand(r["p25"], r["p50"], r["p75"], r["n"])
        ratio = score_by_percentile(r["value"], band, higher_better=meta.higher_better)
        rows.append({"symbol": r["symbol"], "industry": r["industry"], "item": r["item"],
                     "label": meta.label, "module": meta.module, "value": r["value"],
                     "p25": r["p25"], "p50": r["p50"], "p75": r["p75"], "n": r["n"],
                     "ratio": ratio, "points": ratio * meta.max_score,
                     "max_score": meta.max_score})
    return pl.DataFrame(rows, schema=schema).select(cols) if rows else pl.DataFrame(schema=schema)
