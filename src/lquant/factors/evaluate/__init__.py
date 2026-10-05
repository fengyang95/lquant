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
from lquant.factors.evaluate.defaults import DEFAULT_N_GROUPS
from lquant.factors.evaluate.event_study import event_study, event_study_summary
from lquant.factors.evaluate.ic import (
    ic_autocorr,
    ic_by_horizon,
    ic_by_year,
    ic_series,
    ic_summary,
)
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
from lquant.factors.evaluate.sessions import (
    session_ic,
    session_ic_summary,
    session_returns,
)
from lquant.factors.evaluate.trace import trace_periods, trace_snapshot

__all__ = [
    "forward_return", "forward_return_matrix",
    "ic_series", "ic_summary", "ic_by_year", "ic_by_horizon", "ic_autocorr",
    "session_returns", "session_ic", "session_ic_summary",
    "add_quantile", "group_returns", "quantile_nav", "long_short_nav", "quantile_summary",
    "pivot_group_returns", "filter_zscore", "zscore_filter_stats",
    "trace_snapshot", "trace_periods",
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
             n_groups: int = DEFAULT_N_GROUPS, horizons: list[int] | None = None,
             with_report: bool = True, with_robustness: bool = False,
             **kw) -> dict:
    """一次性跑完 IC / 分层 / 衰减 / 归因 / 评级，并可选生成 HTML 报告。

    ``with_robustness=True`` 时额外跑 L3 稳健性（窗口扰动 / 分段稳定 /
    起点敏感 / 月度剔除 / OOS 衰减）。它要按扰动窗口重算因子若干遍，是
    本函数里最贵的一步，所以默认关闭、由调用方显式开启。
    ``expr`` 传给稳健性做参数扰动；缺省时该项记 skipped。
    """
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
    # 评级：把 IC/ICIR/单调性/多空夏普/多重检验校正收敛成「强/中/弱 + 差在哪」
    out["rating"] = factor_rating(out["ic"], out["quantile"],
                                  n_trials=kw.get("n_trials"))
    if with_robustness:
        out["robustness"] = robustness_summary(
            df, factor, ret_col,
            expr=kw.get("expr"), covs=kw.get("covs"),
            icir_is=kw.get("icir_is"), icir_oos=kw.get("icir_oos"),
            **_pick(kw, "deltas", "n_splits", "n_starts", "top_n", "n_groups", "date_col"))
    if with_report:
        # 评级/稳健性在同一个函数体里已经算好 —— 直接喂给报告，
        # 不再出现「算出来了但交付物里看不到」。
        out["report"] = factor_report(
            df, factor, ret_col, n_groups=n_groups, horizons=horizons,
            rating=out.get("rating"), robustness=out.get("robustness"),
            **_pick(kw, "price_col", "date_col", "symbol_col", "cat_col",
                    "group_col", "bps_list", "universe", "filter_zscore",
                    "outlier_stats", "event_window",
                    "display_name", "expr", "data_start", "data_end",
                    "n_samples", "steps", "covariates", "sample_filters",
                    "window", "extras", "description", "errors"))
    return out


def _pick(kw: dict, *keys) -> dict:
    return {k: kw[k] for k in keys if k in kw}
