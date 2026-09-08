"""前瞻收益：评价体系的共同输入。

最容易出错的地方：收益必须**向前对齐**。
`pct_change(n)` 默认算的是「相对前 n 期」，而因子在 T 时刻用 T 日收盘价算出，
能拿到的是 T+1 之后的收益。所以要用 `shift(-h)` 把未来收益搬到当前行。

对齐错了不会报错，只会让 IC 高得离谱 —— 这是新手因子看起来很神的首要原因。
"""
from __future__ import annotations

import polars as pl


def forward_return(df: pl.DataFrame, price_col: str = "close", periods: list[int] | int = 1,
                   *, by: str = "symbol", date_col: str = "trade_date",
                   suffix: str = "fwd") -> pl.DataFrame:
    """在 df 上追加 `fwd_ret_{h}` 列，表示未来 h 期的收益率。

    Parameters
    ----------
    periods : 持有期列表，如 [1, 5, 20]
    by : 分组列（标的代码），确保不会跨标的错位
    """
    if isinstance(periods, int):
        periods = [periods]
    out = df.sort([by, date_col])
    px = pl.col(price_col).cast(pl.Float64)
    for h in periods:
        out = out.with_columns(
            ((px.shift(-h).over(by) / px) - 1.0).alias(f"{suffix}_ret_{h}")
        )
    return out


def forward_return_matrix(df: pl.DataFrame, price_col: str = "close",
                          horizons: list[int] | None = None, **kw) -> pl.DataFrame:
    """常用的一组持有期：1/5/10/20/60。"""
    return forward_return(df, price_col, horizons or [1, 5, 10, 20, 60], **kw)
