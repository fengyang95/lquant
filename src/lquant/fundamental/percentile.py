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
    "BAND_RATIO_LOW",
    "BAND_RATIO_MID",
    "BAND_RATIO_TOP",
    "BELOW_FLOOR",
    "DEFAULT_MIN_SAMPLES",
    "PercentileBand",
    "percentile_bands",
    "percentile_table",
    "score_by_percentile",
    "score_universe",
]

DEFAULT_MIN_SAMPLES = 5

#: 分位档位系数：≥P75 → 1.0，≥P50 → 0.8，≥P25 → 0.5，其余 0.2（反向指标对称）。
#:
#: 这四个数是**评分体系的核心口径**，同时被两条路径消费：
#: :func:`score_by_percentile`（标量，公开 API + 单测）与
#: :func:`score_universe` 里的向量化实现。两边各写一份常量迟早会漂移，
#: 所以只在下面定义一次，向量化实现从同一组常量构造表达式，
#: 并有 ``test_score_universe_matches_scalar_banding`` 逐行交叉验证。
BAND_RATIO_TOP = 1.0
BAND_RATIO_MID = 0.8
BAND_RATIO_LOW = 0.5
BELOW_FLOOR = 0.2


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
            return BAND_RATIO_TOP
        if v >= band.p50:
            return BAND_RATIO_MID
        if v >= band.p25:
            return BAND_RATIO_LOW
        return BELOW_FLOOR
    if v <= band.p25:
        return BAND_RATIO_TOP
    if v <= band.p50:
        return BAND_RATIO_MID
    if v <= band.p75:
        return BAND_RATIO_LOW
    return BELOW_FLOOR


_BAND_SCHEMA = {"industry": pl.String, "item": pl.String, "p25": pl.Float64,
                "p50": pl.Float64, "p75": pl.Float64, "n": pl.Int64}


def _empty_bands() -> pl.DataFrame:
    return pl.DataFrame(schema=_BAND_SCHEMA)


def _long_view(panel: pl.DataFrame, industry: pl.DataFrame, asof: date,
               metrics: Sequence[RatioMetric]) -> pl.DataFrame | None:
    """PIT 宽表 → ``(symbol, industry, item, value)`` 长表。

    抽出来的唯一目的是让 :func:`percentile_table` 与 :func:`score_universe`
    **只做一次** ``resolve_pit`` + unpivot —— 原实现里 ``score_universe``
    调 ``percentile_table``，两者各自把整个面板解析一遍，等于白算一倍。
    """
    items = [m.item for m in metrics]
    wide = resolve_pit(panel, asof, items=items)
    ind = resolve_industry(industry, asof)
    if wide.is_empty() or ind.is_empty():
        return None
    merged = wide.join(ind, on="symbol", how="inner")
    present = [c for c in items if c in merged.columns]
    if not present or not merged.height:
        return None
    return (merged.select(["symbol", "industry", *present])
                  .unpivot(on=present, index=["symbol", "industry"],
                           variable_name="item", value_name="value")
                  .drop_nulls("value"))


def _bands_from_long(long: pl.DataFrame,
                     min_samples: int = DEFAULT_MIN_SAMPLES) -> pl.DataFrame:
    """长表 → 逐 (industry, item) 的 P25/P50/P75（样本不足的组合不出现）。

    向量化：原实现按 (行业, 指标) 分组后逐组把列转成 Python list 再建
    ``pl.Series`` 求分位，31 个行业 × 17 个指标就是 ~500 次解释器往返。
    """
    if long.is_empty():
        return _empty_bands()
    return (
        long.group_by(["industry", "item"])
        .agg(
            pl.col("value").quantile(0.25, interpolation="linear").alias("p25"),
            pl.col("value").quantile(0.50, interpolation="linear").alias("p50"),
            pl.col("value").quantile(0.75, interpolation="linear").alias("p75"),
            pl.len().alias("n"),
        )
        .filter(pl.col("n") >= min_samples)
        .select(["industry", "item", "p25", "p50", "p75", "n"])
        .sort(["industry", "item"])
    )


def percentile_table(panel: pl.DataFrame, industry: pl.DataFrame, asof: date,
                     metrics: Sequence[RatioMetric] = METRICS,
                     min_samples: int = DEFAULT_MIN_SAMPLES) -> pl.DataFrame:
    """在 ``asof`` 日，逐 (行业, 指标) 计算分位。

    Returns:
        长表 ``(industry, item, p25, p50, p75, n)``；样本不足的组合不出现。
    """
    long = _long_view(panel, industry, asof, metrics)
    if long is None:
        return _empty_bands()
    return _bands_from_long(long, min_samples)


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
    if not metrics:
        return pl.DataFrame(schema=schema)

    # 只解析一次：原来 percentile_table 与 score_universe 各解析一遍面板
    long = _long_view(panel, industry, asof, metrics)
    if long is None:
        return pl.DataFrame(schema=schema)
    bands = _bands_from_long(long, min_samples)
    if bands.is_empty():
        return pl.DataFrame(schema=schema)

    # 指标元数据（label / module / max_score / 方向）也走 join，
    # 避免原来逐行 iter_rows 的 Python 循环（全市场 8 万行时是主要耗时）
    meta = pl.DataFrame({
        "item": [m.item for m in metrics],
        "label": [m.label for m in metrics],
        "module": [m.module for m in metrics],
        "max_score": [float(m.max_score) for m in metrics],
        "higher_better": [bool(m.higher_better) for m in metrics],
    })

    v, p25, p50, p75 = (pl.col("value"), pl.col("p25"),
                        pl.col("p50"), pl.col("p75"))
    # 与 score_by_percentile 同一组常量，避免两条路径的口径漂移
    forward = (pl.when(v >= p75).then(BAND_RATIO_TOP)
               .when(v >= p50).then(BAND_RATIO_MID)
               .when(v >= p25).then(BAND_RATIO_LOW)
               .otherwise(BELOW_FLOOR))
    reverse = (pl.when(v <= p25).then(BAND_RATIO_TOP)
               .when(v <= p50).then(BAND_RATIO_MID)
               .when(v <= p75).then(BAND_RATIO_LOW)
               .otherwise(BELOW_FLOOR))
    ratio = pl.when(pl.col("higher_better")).then(forward).otherwise(reverse)

    out = (long.join(bands, on=["industry", "item"], how="inner")
               .join(meta, on="item", how="inner")
               .with_columns(ratio.alias("ratio"))
               .with_columns((pl.col("ratio") * pl.col("max_score")).alias("points")))

    # 比例档位是 1.0/0.8/0.5/0.2 的离散集合，浮点乘法会带出 0.30000000000000004
    # 这类尾巴；四舍五入到 1e-9 让输出稳定可断言。
    return (out.with_columns(pl.col("points").round(9), pl.col("ratio").round(9))
               .select(cols)) if out.height else pl.DataFrame(schema=schema)
