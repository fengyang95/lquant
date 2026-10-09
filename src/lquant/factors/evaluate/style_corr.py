"""中性化后风格相关性：残差因子还偷了多少风格暴露？

研报标准检验（市值/行业中性化后）：因子与各风格因子（市值、动量、换手率…）
的截面相关都应降到低位（如 |ρ| < 0.14）。如果某个风格的相关 still 高，
说明「中性化后 alpha」其实还是那个风格的马甲。

口径：
- 数值风格 → 逐日 Spearman（截面 rank 后 Pearson），取全期均值与最大绝对值。
- 分类风格（如申万行业）→ 逐日相关比 eta（组间方差占比的平方根，0..1），
  衡量因子均值在行业间的离散程度 —— 哑变量相关没有符号，报告绝对量级。
- passed = 所有风格 max|ρ| ≤ threshold。
"""
from __future__ import annotations

import math

import polars as pl

__all__ = ["style_correlation"]

DEFAULT_THRESHOLD = 0.14


def _pearson_expr(x: str, y: str) -> pl.Expr:
    n = pl.len()
    sx, sy = pl.col(x).sum(), pl.col(y).sum()
    sxy = (pl.col(x) * pl.col(y)).sum()
    sxx = (pl.col(x) ** 2).sum()
    syy = (pl.col(y) ** 2).sum()
    num = sxy - sx * sy / n
    den = ((sxx - sx * sx / n) * (syy - sy * sy / n)).sqrt()
    return pl.when(den > 0).then(num / den).otherwise(None).alias("corr")


def _daily_spearman(d: pl.DataFrame, factor_col: str, style_col: str,
                    date_col: str) -> pl.DataFrame:
    """逐日 Spearman：截面 rank（average 处理并列）→ 逐日 Pearson of ranks。

    rank 前过滤非有限值：polars rank 把 NaN 顶格排在所有有限值之上，
    不过滤的话中性化体检的 |ρ| 会被「因子无效」的股票污染甚至翻转结论。
    """
    dd = d.select([date_col, factor_col, style_col]).drop_nulls().rename(
        {style_col: "_s"})
    dd = dd.filter(pl.col(factor_col).is_finite() & pl.col("_s").is_finite())
    dd = dd.with_columns([
        pl.col(factor_col).rank("average").over(date_col).alias("_rf"),
        pl.col("_s").rank("average").over(date_col).alias("_rs"),
    ])
    return dd.group_by(date_col).agg(_pearson_expr("_rf", "_rs")).drop_nulls()


def _daily_eta(d: pl.DataFrame, factor_col: str, group_col: str,
               date_col: str) -> pl.DataFrame:
    """逐日相关比 eta = sqrt(1 - SS_within / SS_total)：因子均值在组间的离散度。

    同 _daily_spearman：先过滤非有限值，一个 NaN 会把整日 ss_tot 染成 NaN
    （该日样本静默蒸发）。
    """
    dd = d.select([date_col, factor_col, group_col]).drop_nulls().rename(
        {group_col: "_g"})
    dd = dd.filter(pl.col(factor_col).is_finite())
    tot = dd.group_by(date_col).agg(
        (pl.col(factor_col) - pl.col(factor_col).mean()).pow(2).sum().alias("ss_tot"))
    dd = dd.with_columns(
        pl.col(factor_col).mean().over([date_col, "_g"]).alias("_gm"))
    wit = (dd.with_columns((pl.col(factor_col) - pl.col("_gm")).pow(2).alias("_w"))
           .group_by(date_col).agg(pl.col("_w").sum().alias("ss_w")))
    j = tot.join(wit, on=date_col, how="inner")
    return j.with_columns(
        pl.when(pl.col("ss_tot") > 0)
        .then((1.0 - pl.col("ss_w") / pl.col("ss_tot")).clip(0.0, 1.0).sqrt())
        .otherwise(None).alias("corr")
    ).drop_nulls()


def style_correlation(df: pl.DataFrame, factor_col: str,
                      style_cols: list[str], *, date_col: str = "trade_date",
                      group_col: str | None = None,
                      threshold: float = DEFAULT_THRESHOLD,
                      min_obs_per_day: int = 5) -> dict:
    """中性化后因子 vs 各风格的相关性体检。

    Returns
    -------
    dict: {styles: [{style, kind, corr_mean, corr_abs_max, passed}],
           threshold, max_abs, passed, n_days}
    """
    out_styles = []
    for c in style_cols:
        if c not in df.columns:      # 缺列不上报毒化值：corr=None、passed=None
            out_styles.append({"style": c, "kind": "numeric",
                               "corr_mean": None, "corr_abs_max": None, "passed": None})
            continue
        s = _daily_spearman(df, factor_col, c, date_col)
        if not len(s):
            out_styles.append({"style": c, "kind": "numeric",
                               "corr_mean": None, "corr_abs_max": None, "passed": None})
            continue
        mean_v = float(s["corr"].mean())
        absmax = float(s["corr"].abs().max())
        out_styles.append({"style": c, "kind": "numeric", "corr_mean": mean_v,
                           "corr_abs_max": absmax, "passed": bool(absmax <= threshold)})
    if group_col and group_col in df.columns:
        e = _daily_eta(df, factor_col, group_col, date_col)
        if len(e):
            absmax = float(e["corr"].abs().max())
            out_styles.append({"style": group_col, "kind": "categorical(eta)",
                               "corr_mean": float(e["corr"].mean()),
                               "corr_abs_max": absmax, "passed": bool(absmax <= threshold)})
        else:
            out_styles.append({"style": group_col, "kind": "categorical(eta)",
                               "corr_mean": None, "corr_abs_max": None, "passed": None})
    absmaxes = [s["corr_abs_max"] for s in out_styles
                if s["corr_abs_max"] is not None and math.isfinite(s["corr_abs_max"])]
    return {
        "styles": out_styles,
        "threshold": threshold,
        "max_abs": max(absmaxes) if absmaxes else None,
        "passed": bool(absmaxes) and all(m <= threshold for m in absmaxes),
    }
