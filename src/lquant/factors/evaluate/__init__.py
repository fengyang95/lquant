"""因子评价：IC / 分层 / 衰减 / 归因 / 报告。

典型用法：

    from lquant.factors.evaluate import forward_return, evaluate, save_report
    d = forward_return(df, "close", periods=[1, 5, 20])
    res = evaluate(d, "mom20")
    save_report(res["report"], "reports/mom20.html")

所有模块都以「前瞻收益已对齐」为前提 —— 对齐错了 IC 会虚高，
所以入口统一从 `forward_return` 开始。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.evaluate.attribution import attribution_summary, exposure
from lquant.factors.evaluate.decay import decay_profile, decay_summary, half_life, suggest_rebalance
from lquant.factors.evaluate.event_study import event_study, event_study_summary
from lquant.factors.evaluate.ic import ic_autocorr, ic_by_year, ic_series, ic_summary
from lquant.factors.evaluate.outliers import (
    filter_zscore,
    zscore_filter_stats,
    zscore_filter_with_stats,
)
from lquant.factors.evaluate.quantile import (
    add_quantile,
    group_returns,
    long_short_nav,
    pivot_group_returns,
    quantile_nav,
    quantile_summary,
)
from lquant.factors.evaluate.rating import RatingThresholds, factor_rating, load_thresholds
from lquant.factors.evaluate.report import factor_report, save_report
from lquant.factors.evaluate.returns import forward_return, forward_return_matrix
from lquant.factors.evaluate.robustness import (
    best_month_removal,
    oos_decay,
    param_sensitivity,
    robustness_summary,
    start_date_sensitivity,
    time_stability,
)

__all__ = [
    "forward_return", "forward_return_matrix",
    "ic_series", "ic_summary", "ic_by_year", "ic_autocorr",
    "add_quantile", "group_returns", "quantile_nav", "long_short_nav", "quantile_summary",
    "pivot_group_returns", "filter_zscore", "zscore_filter_stats",
    "zscore_filter_with_stats",
    "event_study", "event_study_summary",
    "decay_profile", "decay_summary", "half_life", "suggest_rebalance",
    "attribution_summary", "exposure",
    "RatingThresholds", "factor_rating", "load_thresholds",
    "param_sensitivity", "time_stability", "start_date_sensitivity",
    "best_month_removal", "oos_decay", "robustness_summary",
    "factor_report", "save_report", "evaluate",
]


def evaluate(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
             n_groups: int = 10, horizons: list[int] | None = None,
             with_report: bool = True, **kw) -> dict:
    """一次性跑完 IC / 分层 / 衰减 / 归因，并可选生成 HTML 报告。"""
    out = {
        "factor": factor,
        "ic": ic_summary(df, factor, ret_col, **_pick(kw, "date_col", "min_obs")),
        "quantile": quantile_summary(df, factor, ret_col, n_groups,
                                     **_pick(kw, "date_col", "periods_per_year")),
        "decay": decay_summary(df, factor, horizons, **_pick(kw, "price_col", "date_col",
                                                             "symbol_col")),
        "attribution": attribution_summary(df, factor, ret_col,
                                           **_pick(kw, "date_col", "cat_col", "num_cols")),
    }
    out["ic"].pop("series", None)      # 序列太大，不放进汇总 dict，需要时单独调 ic_series
    if with_report:
        out["report"] = factor_report(df, factor, ret_col, n_groups=n_groups,
                                      horizons=horizons, **_pick(kw, "price_col", "date_col",
                                                                 "symbol_col", "cat_col",
                                                                 "universe", "filter_zscore",
                                                                 "outlier_stats", "event_window"))
    return out


def _pick(kw: dict, *keys) -> dict:
    return {k: kw[k] for k in keys if k in kw}
