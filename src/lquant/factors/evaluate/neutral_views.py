"""§5.1 三件事：三种「中性化」数学不等价，页面上必须标明用的哪种。

- factor_neutral_ic（默认）：因子~协变量回归取残差再算 IC —— 现有 pipeline
- return_neutral_ic：收益~协变量回归，因子对**残差收益**算 IC —— 对照视图
- industry_group_quantile：分层回测**行业内分组**组内选股 —— 分层选项
"""
from __future__ import annotations

import polars as pl

from lquant.factors.evaluate.ic import ic_series
from lquant.factors.preprocess._regress import (
    residual_by_day,
)


def return_neutral_ic(df: pl.DataFrame, factor: str, ret_col: str,
                      covariates: list[str]) -> pl.DataFrame:
    """收益中性化：逐日 ret~covariates 回归，返回带 ret_neutral 残差收益列的 df。

    因子对残差收益算 IC 即「收益中性化 IC」（对照视图，与因子值中性化不等价）。
    """
    def _solve(X, y):
        import numpy as np

        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        return y - X @ beta

    out = residual_by_day(df, ret_col, "trade_date", covariates, _solve)
    return out.rename({ret_col: "ret_neutral"})


def industry_group_quantile(df: pl.DataFrame, factor: str, ret_col: str,
                            group_col: str, n_groups: int = 5) -> pl.DataFrame:
    """行业内分组：在 [trade_date, 行业] 组内按因子分位，返回带 q 列的 df。"""
    if group_col not in df.columns:
        raise ValueError(f"行业内分组需要行业列 {group_col}（cov_industry_sw1）")
    # 组内分位桶：[trade_date, 行业] 组内 rank → 1..n_groups 桶
    return df.with_columns(
        ((((pl.col(factor).rank().over(["trade_date", group_col]) - 1)
           * n_groups) // pl.col(factor).count().over(["trade_date", group_col])) + 1)
        .cast(pl.Int64).alias("q"))


def neutral_views(df: pl.DataFrame, factor: str, ret_col: str,
                  covariates: list[str] | None = None,
                  group_col: str | None = None, n_groups: int = 5) -> dict:
    """三种中性化视图汇总（页面上必须标明用的哪种，否则数字没法对话）。"""
    out = {"view": "factor_neutral (默认: 因子~协变量取残差再算 IC)"}
    if covariates:
        r = return_neutral_ic(df, factor, ret_col, covariates)
        s = ic_series(r.drop_nulls([factor, "ret_neutral"]), factor, "ret_neutral")
        if len(s):
            out["return_neutral_ic"] = round(float(s["ic"].mean()), 4)
    if group_col:
        industry_group_quantile(df, factor, ret_col, group_col, n_groups)
        out["industry_group_quantile"] = True
    return out
