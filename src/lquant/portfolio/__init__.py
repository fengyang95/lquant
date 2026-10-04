"""组合层：选池 → 去重 → 权重。

三步流水线的顺序是有讲究的：
先选池（去掉根本不能买的），再去重（去掉重复的暴露），最后配权重（决定押多少）。
反过来做会在同质标的之间反复分配权重，白白损失分散度。

    from lquant.portfolio import screen, dedup, weights
    picks = screen(df, {"mom20": 0.6, "vol20": -0.4}, top_n=50)
    kept  = dedup(returns_wide, list(picks["symbol"]), threshold=0.85,
                  priority=picks)
    w     = weights(returns_wide[kept], method="hrp")
"""
from __future__ import annotations

from lquant.portfolio.dedup import (
    cluster_labels,
    cluster_summary,
    correlation_matrix,
    dedup,
)
from lquant.portfolio.optimizer import (
    EnhancedIndexingResult,
    OptimizerError,
    active_share,
    enhanced_indexing_weight,
    tracking_error,
)
from lquant.portfolio.riskmodel import (
    COV_ESTIMATORS,
    condition_number,
    estimate_cov,
    is_psd,
    nearest_psd,
    poet_cov,
    sample_cov,
    shrink_cov,
    structured_cov,
)
from lquant.portfolio.screener import (
    FilterConfig,
    apply_filters,
    filter_report,
    score,
    screen,
)
from lquant.portfolio.weighting import (
    METHODS,
    equal_weight,
    hrp_weight,
    inverse_vol_weight,
    market_cap_weight,
    min_variance_weight,
    risk_parity_weight,
    score_weight,
    weight_report,
    weights,
)

__all__ = [
    "FilterConfig", "apply_filters", "filter_report", "screen", "score",
    "dedup", "cluster_labels", "cluster_summary", "correlation_matrix",
    "weights", "METHODS", "equal_weight", "score_weight", "market_cap_weight",
    "inverse_vol_weight", "risk_parity_weight", "min_variance_weight", "hrp_weight",
    "weight_report",
    # 风险模型（Phase 3.1）
    "COV_ESTIMATORS", "estimate_cov", "sample_cov", "shrink_cov",
    "structured_cov", "poet_cov", "condition_number", "is_psd", "nearest_psd",
    # 基准相对优化（Phase 3.2）
    "enhanced_indexing_weight", "EnhancedIndexingResult", "OptimizerError",
    "tracking_error", "active_share",
]
