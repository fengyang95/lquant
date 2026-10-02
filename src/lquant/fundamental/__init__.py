"""基本面评分层：行业相对分位 + 三表勾稽。

与因子层的关系：因子层做**横截面排序**（``Rank`` / ``ZScore``），
本层做**行业内相对位置**。A 股的基本面比率（毛利率、存货天数、资产负债率）
跨行业绝对不可比，行业分位是横截面排序无法替代的一层。

三层结构：

- :mod:`lquant.fundamental.panel` —— ``financial_pit`` 的 PIT 解析
  （强制 ``pub_date <= asof``，无公告日的行永不返回）
- :mod:`lquant.fundamental.percentile` —— 逐 (行业, 指标) 分位与打分
- :mod:`lquant.fundamental.score` —— 聚合总分、评级、**覆盖率**
- :mod:`lquant.fundamental.reconcile` —— 三表勾稽（独立于总分的质量检查）

典型用法::

    from lquant.fundamental import score_history

    # 在每个调仓日滚动打分（而不是钉死某一天的截面）
    scores = score_history(financial_pit, industry_classify, rebalance_dates)
"""
from __future__ import annotations

from lquant.fundamental.metrics import (
    METRICS,
    MODULE_LABELS,
    MODULE_WEIGHTS,
    RatioMetric,
    metrics_by_module,
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
    rating,
    score_history,
    score_snapshot,
)

__all__ = [
    "DEFAULT_FULL_SCORES",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_RATING_THRESHOLDS",
    "DEFAULT_TIERS",
    "METRICS",
    "MODULE_LABELS",
    "MODULE_WEIGHTS",
    "PASS_TIER",
    "PercentileBand",
    "RatioMetric",
    "ReconciliationItem",
    "ReconciliationReport",
    "aggregate",
    "banded_score",
    "cash_change_gap",
    "earnings_quality_gap",
    "metrics_by_module",
    "ocf_to_ni_ratio",
    "percentile_bands",
    "percentile_table",
    "rating",
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
]
