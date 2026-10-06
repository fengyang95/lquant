"""§5.1 三件事：三种「中性化」数学不等价，页面上必须标明用的哪种。

- factor_neutral_ic（默认）：因子~协变量回归取残差再算 IC —— 现有 pipeline
- return_neutral_ic：收益~协变量回归，因子对**残差收益**算 IC —— 对照视图
- industry_group_quantile：分层回测**行业内分组**组内选股 —— 分层选项
"""
from __future__ import annotations

import math

import polars as pl

from lquant.factors.evaluate.defaults import DEFAULT_N_GROUPS
from lquant.factors.evaluate.ic import ic_series
from lquant.factors.preprocess._regress import (
    residual_by_day,
)

__all__ = ["return_neutral_ic", "industry_group_quantile",
           "industry_group_quantile_summary", "neutral_views"]


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
    """行业内分组：在 [trade_date, 行业] 组内按因子分位，返回带 q 列的 df。

    NaN 不是 null：polars rank 会把 NaN 排到最大（与 ``add_quantile`` 同款防护），
    不挡的话 NaN 行全进最高桶且 count() 分母被计入。
    """
    if group_col not in df.columns:
        raise ValueError(f"行业内分组需要行业列 {group_col}（cov_industry_sw1）")
    key = ["trade_date", group_col]
    fin = pl.col(factor).is_finite()
    cnt = fin.sum().over(key)
    q = (pl.when(fin)
         .then(((pl.col(factor).rank().over(key) - 1) * n_groups) / cnt)
         .otherwise(None)
         .floor() + 1)
    return df.with_columns(q.cast(pl.Int64).clip(1, n_groups).alias("q"))


def industry_group_quantile_summary(df: pl.DataFrame, factor: str, ret_col: str,
                                    group_col: str, n_groups: int = 5, *,
                                    date_col: str = "trade_date") -> dict:
    """行业内分组分层的**结果**（不是标记）：各组平均收益 + 首尾差 + 单调性。

    这是 §5.1 的第三种视图，与前两种数学不等价：它既不改因子值（不同于
    factor_neutral），也不改收益（不同于 return_neutral_ic），而是把选股
    限制在行业内部 —— 因此不受行业 beta 干扰，也不把行业信息从因子里抹掉。
    返回 ``insufficient=True`` 表示样本不足，调用方应显示「样本不足」而非空表。
    """
    from lquant.factors.evaluate.quantile import _spearman

    empty = {"n_groups": n_groups, "groups": [], "top_bottom_spread": float("nan"),
             "monotonicity": float("nan"), "n_obs": 0, "insufficient": True}
    if group_col not in df.columns or ret_col not in df.columns or factor not in df.columns:
        return empty
    gq = industry_group_quantile(df, factor, ret_col, group_col, n_groups)
    d = gq.drop_nulls(["q", ret_col])
    if not len(d):
        return empty
    agg = (d.group_by("q")
           .agg([pl.len().alias("n"), pl.col(ret_col).mean().alias("mean_ret")])
           .sort("q"))
    groups = [{"q": int(r["q"]), "n": int(r["n"]), "mean_ret": float(r["mean_ret"])}
              for r in agg.to_dicts() if r["mean_ret"] is not None]
    if len(groups) < 2:
        return {**empty, "groups": groups, "n_obs": len(d)}
    pairs = [(g["q"], g["mean_ret"]) for g in groups if math.isfinite(g["mean_ret"])]
    mono = _spearman([q for q, _ in pairs], [m for _, m in pairs]) if len(pairs) >= 3 \
        else float("nan")
    return {
        "n_groups": n_groups,
        "groups": groups,
        "top_bottom_spread": pairs[-1][1] - pairs[0][1] if len(pairs) >= 2 else float("nan"),
        "monotonicity": mono,
        "n_obs": len(d),
        "insufficient": False,
    }


def neutral_views(df: pl.DataFrame, factor: str, ret_col: str,
                  covariates: list[str] | None = None,
                  group_col: str | None = None,
                  n_groups: int = DEFAULT_N_GROUPS) -> dict:
    """三种中性化视图汇总（页面上必须标明用的哪种，否则数字没法对话）。"""
    out = {"view": "factor_neutral (默认: 因子~协变量取残差再算 IC)"}
    if covariates:
        r = return_neutral_ic(df, factor, ret_col, covariates)
        s = ic_series(r.drop_nulls([factor, "ret_neutral"]), factor, "ret_neutral")
        if len(s):
            out["return_neutral_ic"] = round(float(s["ic"].mean()), 4)
    if group_col:
        # 返回真实结果而非 True 标记 —— 只给布尔值等于「能力存在但没数据」
        out["industry_group_quantile"] = industry_group_quantile_summary(
            df, factor, ret_col, group_col, n_groups)
    return out
