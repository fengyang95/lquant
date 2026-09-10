"""滚动窗口因子指标：滚动 IC / RankIC / IR / 胜率。

全样本 IC 好看的因子可能近一年已经失效 —— 滚动指标把「什么时候失效」
显式画出来。窗口在有效交易日序列上滚动：ic_series 会剔除样本数不足
min_obs 的日期，滚动窗口覆盖的是这些有效日。
"""

from __future__ import annotations

import polars as pl

from lquant.factors.evaluate.ic import ic_series

__all__ = ["rolling_ic"]


def rolling_ic(
    df: pl.DataFrame,
    factor: str,
    ret_col: str = "fwd_ret_1",
    window: int = 60,
    *,
    date_col: str = "trade_date",
) -> pl.DataFrame:
    """滚动窗口 IC 汇总，窗口终点对齐到日期列。

    返回列：date_col, ic_mean, rank_ic_mean, ir, positive_rate, n_days。
    数据不足一个窗口时返回空表。
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got: {window}")
    s = ic_series(df, factor, ret_col, date_col=date_col)
    if not len(s):
        return s
    if len(s) < window:
        return s.head(0)
    out = (
        s.with_row_index("_i")
        .rolling(index_column="_i", period=f"{window}i")
        .agg(
            [
                pl.last(date_col),
                pl.col("ic").mean().alias("ic_mean"),
                pl.col("rank_ic").mean().alias("rank_ic_mean"),
                pl.col("ic").std().alias("ic_std"),
                (pl.col("ic") > 0).mean().alias("positive_rate"),
                pl.len().alias("n_days"),
            ]
        )
        .drop_nulls("ic_mean")
        .with_columns(
            (pl.col("ic_mean") / pl.col("ic_std")).alias("ir"),
        )
        .drop("ic_std")
        .select([date_col, "ic_mean", "rank_ic_mean", "ir", "positive_rate", "n_days"])
        # rolling 对前 window-1 个部分窗口也出值，切掉，只留完整窗口
        .slice(window - 1)
    )
    return out
