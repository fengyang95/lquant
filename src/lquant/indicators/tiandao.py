"""天道通道指标（金牛 / 金钻）—— **右对齐截断 XMA，不使用未来数据**。

公式（来自通达信公式习惯用法）::

    金牛（通道上轨）= 2 · XMA²(high, n) − XMA²(low, n)
    金钻（通道下轨）= 2 · XMA²(low,  n) − XMA²(high, n)
    XMA²(X, n)      = XMA(XMA(X, n), n)

**与原始实现的唯一区别（也是本模块存在的理由）**：通达信 ``XMA`` 是
**居中对称**滑动平均，在 i 处取 ``[i−h, i+h−ε]``（``h = n//2``）的均值，
即 **引用了未来 h 根数据**，尾部数值会随新数据到来而漂移。

很多移植版本直接照搬居中窗口，于是日线选股/回测出现前视偏差
（回测收益虚高、实盘失效）。本实现改为**右对齐截断**：

    XMA_trunc(X, n)[i] = mean(X[max(0, i−h) : i+1])

等价于 ``rolling_mean(window_size=h+1, min_samples=1)`` —— 向量化、无 Python 循环，
且满足 ``lquant.indicators.future`` 的前缀不变性判据（见
``tests/unit/test_indicators_future.py`` 中的反向对照用例）。

注意口径差异：截断版因为看不到右侧，曲线与看盘软件的居中版**在尾部若干根会不同**，
这是**正确性的代价**，不是 bug。
"""
from __future__ import annotations

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["xma_half_window", "xma_truncated", "add_tiandao",
           "TIANDAO_DEFAULT_N"]

TIANDAO_DEFAULT_N = 25


def xma_half_window(n: int) -> int:
    """居中窗口的半宽 ``h``；截断版据此确定回看根数 ``h+1``。"""
    if n < 1:
        raise ValueError("n 必须为正整数")
    return n // 2


def xma_truncated(col: str | pl.Expr, n: int) -> pl.Expr:
    """右对齐截断 XMA 表达式（只看当前及之前 ``h`` 根）。

    Args:
        col: 列名或已有的 Polars 表达式（便于嵌套调用）。
        n: 周期。
    """
    expr = pl.col(col) if isinstance(col, str) else col
    h = xma_half_window(n)
    return expr.rolling_mean(window_size=h + 1, min_samples=1)


@register_indicator("tiandao", label="天道通道（金牛/金钻）", category="channel",
                    min_window=60, inputs=("high", "low", "close"),
                    outputs=("td_jinniu", "td_jinzuan", "td_gold_buy", "td_gold_sell"))
def add_tiandao(df: pl.DataFrame, n: int = TIANDAO_DEFAULT_N) -> pl.DataFrame:
    """叠加金牛 / 金钻通道与两个信号列。"""
    xma2_high = xma_truncated(xma_truncated("high", n), n)
    xma2_low = xma_truncated(xma_truncated("low", n), n)
    df = df.with_columns(
        (2 * xma2_high - xma2_low).alias("td_jinniu"),
        (2 * xma2_low - xma2_high).alias("td_jinzuan"),
    )
    df = df.with_columns(
        (pl.col("close") < pl.col("td_jinzuan")).alias("td_gold_buy"),
        (pl.col("close") > pl.col("td_jinniu")).alias("td_gold_sell"),
    )
    return df
