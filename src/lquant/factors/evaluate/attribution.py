"""归因：行业 / 市值 / 流动性暴露分解。

回答两个问题：
1. 这个因子到底在赌什么？（暴露）
2. 收益是从哪来的？（贡献）

多空组合的行业暴露若不接近 0，说明因子收益其实是行业 beta，
中性化没做干净 —— 这类因子一旦行业轮动就会大幅回撤。
"""
from __future__ import annotations

import polars as pl

__all__ = ["exposure", "group_exposure", "return_contribution", "attribution_summary"]


def exposure(df: pl.DataFrame, factor: str, by: str = "industry_sw1",
             *, date_col: str = "trade_date", top: float = 0.2,
             bottom: float = 0.2) -> pl.DataFrame:
    """多头组 vs 空头组在 `by` 维度上的权重差（暴露）。

    按分位取前 top 与后 bottom 比例的持仓，比较两组的行业分布。
    """
    if by not in df.columns:
        return pl.DataFrame()

    ranked = df.with_columns(
        rk=pl.col(factor).rank("ordinal").over(date_col) / pl.col(factor).count().over(date_col)
    )
    hi = ranked.filter(pl.col("rk") > 1 - top)
    lo = ranked.filter(pl.col("rk") <= bottom)

    def dist(part: pl.DataFrame) -> dict[str, float]:
        g = part.group_by(by).agg(pl.len().alias("n")).with_columns(
            w=pl.col("n") / pl.col("n").sum())
        return dict(zip(g[by].cast(pl.Utf8).to_list(), g["w"].to_list(), strict=False))

    dh, dl = dist(hi), dist(lo)
    keys = sorted(set(dh) | set(dl))
    return pl.DataFrame({
        by: keys,
        "weight_long": [dh.get(k, 0.0) for k in keys],
        "weight_short": [dl.get(k, 0.0) for k in keys],
    }).with_columns(exposure=pl.col("weight_long") - pl.col("weight_short")).sort(
        pl.col("exposure").abs(), descending=True)


def group_exposure(df: pl.DataFrame, factor: str, cols: list[str],
                   *, date_col: str = "trade_date",
                   n_groups: int = 5) -> pl.DataFrame:
    """每个因子分位组在若干连续变量上的均值（市值、换手率等）。

    看的是「第 10 组是不是全是小票」这类问题。
    """
    from lquant.factors.evaluate.quantile import add_quantile

    d = add_quantile(df, factor, n_groups, date_col=date_col)
    aggs = [pl.col(c).cast(pl.Float64, strict=False).mean().alias(c) for c in cols if c in df.columns]
    if not aggs:
        return pl.DataFrame()
    return d.group_by("q").agg([pl.len().alias("n"), *aggs]).sort("q")


def return_contribution(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                        by: str = "industry_sw1", *, date_col: str = "trade_date",
                        n_groups: int = 10) -> pl.DataFrame:
    """按维度分解收益贡献：组内权重 × 组内平均收益。"""
    from lquant.factors.evaluate.quantile import add_quantile

    if by not in df.columns:
        return pl.DataFrame()
    d = add_quantile(df, factor, n_groups, date_col=date_col).drop_nulls([ret_col, by])
    if not len(d):
        return pl.DataFrame()

    total = d[ret_col].mean()
    g = d.group_by(by).agg([
        pl.len().alias("n"),
        pl.col(ret_col).mean().alias("mean_ret"),
    ]).with_columns(
        weight=pl.col("n") / pl.col("n").sum(),
        total_ret=pl.lit(total),
    )
    return g.with_columns(
        contribution=pl.col("weight") * (pl.col("mean_ret") - pl.col("total_ret"))
    ).sort("contribution", descending=True)


def attribution_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                        *, date_col: str = "trade_date",
                        cat_col: str = "industry_sw1",
                        num_cols: list[str] | None = None) -> dict:
    """归因汇总：行业暴露 + 分组特征 + 收益贡献。"""
    num_cols = num_cols or [c for c in ("market_cap", "turnover_rate") if c in df.columns]
    exp = exposure(df, factor, by=cat_col, date_col=date_col)
    gross = float(exp["exposure"].abs().sum()) if len(exp) else float("nan")
    return {
        "factor": factor,
        "industry_exposure": exp,
        "gross_exposure": gross,            # 越接近 0 说明行业越中性
        "group_profile": group_exposure(df, factor, num_cols, date_col=date_col),
        "contribution": return_contribution(df, factor, ret_col, by=cat_col, date_col=date_col),
    }
