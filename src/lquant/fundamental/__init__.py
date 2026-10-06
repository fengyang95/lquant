"""基本面评分层：行业相对分位 + 三表勾稽。

与因子层的关系：因子层做**横截面排序**（``Rank`` / ``ZScore``），
本层做**行业内相对位置**。A 股的基本面比率（毛利率、存货天数、资产负债率）
跨行业绝对不可比，行业分位是横截面排序无法替代的一层。

层次结构（指标值的三个来源是**互相独立**的数据源，任一缺失都会让某些模块恒 0 分）：

- :mod:`lquant.fundamental.metrics` —— 指标目录：模块权重、方向、**取数来源**
- :mod:`lquant.fundamental.derive` —— 派生指标：库里没有现成比率的，由原始
  科目按**同一报告期**现算
- :mod:`lquant.fundamental.valuation` —— 估值指标：从**日线湖**取 PE/PB/股息率，
  负值（亏损 / 净资产为负）不计分
- :mod:`lquant.fundamental.panel` —— ``financial_pit`` 的 PIT 解析
  （强制 ``pub_date <= asof``，无公告日的行永不返回）
- :mod:`lquant.fundamental.percentile` —— 逐 (行业, 指标) 分位与打分
- :mod:`lquant.fundamental.score` —— 聚合总分、评级、**覆盖率**、模块可得性
- :mod:`lquant.fundamental.reconcile` —— 三表勾稽（独立于总分的质量检查）

调用方需要自己把三个来源拼成长表再交给打分::

    pit = <读 financial_pit 的 pit_items() 与 raw_items()>
    panel = pl.concat([pit, derive_pit(pit), valuation_long(asof)])
    detail = score_universe(panel, industry, asof)

典型用法::

    from lquant.fundamental import score_history

    # 在每个调仓日滚动打分（而不是钉死某一天的截面）
    scores = score_history(financial_pit, industry_classify, rebalance_dates)
"""
from __future__ import annotations

from lquant.fundamental.derive import (
    DERIVED_METRICS,
    DerivedSpec,
    derive_pit,
    derived_labels,
    raw_items,
)
from lquant.fundamental.metrics import (
    METRICS,
    MODULE_LABELS,
    MODULE_WEIGHTS,
    SOURCES,
    RatioMetric,
    metrics_by_module,
    metrics_by_module_of,
    pit_items,
    total_weight,
    validate_modules,
)
from lquant.fundamental.panel import resolve_industry, resolve_pit
from lquant.fundamental.percentile import (
    DEFAULT_MIN_SAMPLES,
    PercentileBand,
    percentile_bands,
    percentile_table,
    score_by_percentile,
    score_universe,
)
from lquant.fundamental.reconcile import (
    DEFAULT_FULL_SCORES,
    DEFAULT_TIERS,
    PASS_TIER,
    ReconciliationItem,
    ReconciliationReport,
    banded_score,
    cash_change_gap,
    earnings_quality_gap,
    ocf_to_ni_ratio,
    reconcile,
    retained_earnings_gap,
)
from lquant.fundamental.score import (
    DEFAULT_RATING_THRESHOLDS,
    aggregate,
    module_coverage,
    rating,
    score_history,
    score_snapshot,
)
from lquant.fundamental.valuation import (
    VALUATION_ITEMS,
    valuation_frame,
    valuation_items,
    valuation_long,
)

__all__ = [
    "DEFAULT_FULL_SCORES",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_RATING_THRESHOLDS",
    "DEFAULT_TIERS",
    "DERIVED_METRICS",
    "DerivedSpec",
    "METRICS",
    "MODULE_LABELS",
    "MODULE_WEIGHTS",
    "PASS_TIER",
    "PercentileBand",
    "RatioMetric",
    "ReconciliationItem",
    "ReconciliationReport",
    "SOURCES",
    "VALUATION_ITEMS",
    "aggregate",
    "banded_score",
    "cash_change_gap",
    "derive_pit",
    "derived_labels",
    "earnings_quality_gap",
    "metrics_by_module",
    "metrics_by_module_of",
    "module_coverage",
    "ocf_to_ni_ratio",
    "percentile_bands",
    "percentile_table",
    "pit_items",
    "rating",
    "raw_items",
    "reconcile",
    "resolve_industry",
    "resolve_pit",
    "retained_earnings_gap",
    "score_by_percentile",
    "score_history",
    "score_snapshot",
    "score_universe",
    "total_weight",
    "validate_modules",
    "valuation_frame",
    "valuation_items",
    "valuation_long",
]
