"""筹码分布 CYQ（Cost Yang Qu? —— 业内通称 Chip/筹码）：成本分布与获利盘比例。

借鉴 InStock（14.7k）的 kline/cyq.py 思路，按 lquant 风格重写为 Polars 列式：

**换手率衰减模型**（CYQ 的通行算法）：
  每个交易日，历史筹码按当日换手率 ``turnover = volume / float_shares`` 被部分
  换手 —— 旧筹码留存 ``(1 - turnover)``，新筹码 ``turnover`` 落在当日成交价附近。
  迭代到最后，得到「按价格分布的持仓成本」直方图：
      dist = dist * (1 - t) + t * day_price_bin

**为什么要 float_shares**：换手率的分母是流通股本。A 股数据源普遍不直接给
``float_shares``，本模块两种口径（按优先级）：
  1. 调用方显式传 ``float_shares``（股数）—— 最准；
  2. 缺省用 ``amount / close``（当日成交额/收盘价 ≈ 当日成交股数）做代理
     —— 隐含「当日换手 100%」的激进假设，衰减过快，**只用于粗筛与演示**，
     evidence 里会明确标注代理口径，绝不冒充精确值（不静默哲学）。

输出：
  - ``cost_distribution(df, bins)``：末端成本分布（bin 中心 → 概率和 1）
  - ``profit_ratio(df)``：获利盘比例 = 现价之上成本占比（0~1）
"""
from __future__ import annotations

import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["cost_distribution", "profit_ratio", "CYQ_PROXY_NOTE"]

CYQ_PROXY_NOTE = "float_shares 用 amount/close 代理（当日换手≈100% 假设），仅作粗筛口径"


def _price_col(df: pl.DataFrame) -> pl.Expr:
    """当日成交代表价：经典 CYQ 用 (H+L+2C)/4 加权 —— 比单 close 更贴真实成交重心。"""
    return (pl.col("high") + pl.col("low") + 2 * pl.col("close")) / 4


def _turnover(df: pl.DataFrame, float_shares: float | None) -> pl.Series:
    if float_shares and float_shares > 0:
        return (df["volume"] / float_shares).clip(0.0, 1.0)
    # 代理口径：amount/close ≈ 成交股数 → 相对「自身」的换手恒为 1。
    # 改用量能相对均量的归一：放量日换手多、缩量日少，保留相对结构。
    vol = df["volume"].to_list()
    ma = sum(vol) / len(vol)
    return pl.Series([min(v / ma, 1.0) for v in vol])


def cost_distribution(df: pl.DataFrame, bins: int = 60,
                      float_shares: float | None = None
                      ) -> list[tuple[float, float]]:
    """末端成本分布。返回 [(bin 中心价, 概率)]，概率和为 1。

    迭代：每根 K 线后 ``dist = dist*(1-t) + t*day_bin``。dist 用等宽价格格
    （min(low)..max(high)），复用 Polars 的 hist 语义太重，纯 Python 循环
    bins 次 —— bins 是常数量级（默认 60），性能瓶颈不在这里。
    """
    if df.is_empty():
        return []
    px = df.with_columns(_price_col(df).alias("_px"))
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        hi = lo + 1e-9
    width = (hi - lo) / bins
    dist = [0.0] * bins
    ts = _turnover(df, float_shares).to_list()
    centers = [(lo + (i + 0.5) * width) for i in range(bins)]

    for row, t in zip(px.iter_rows(named=True), ts):
        p = row["_px"]
        idx = min(int((p - lo) / width), bins - 1)
        for i in range(bins):
            dist[i] *= (1 - t)
        dist[idx] += t
    total = sum(dist) or 1.0
    return [(centers[i], dist[i] / total) for i in range(bins) if dist[i] > 0]


def profit_ratio(df: pl.DataFrame, float_shares: float | None = None) -> dict:
    """获利盘比例：成本低于现价的筹码占比（0~1）。>0.9 常被视为「高位获利盘重」。"""
    last = float(df["close"][-1])
    dist = cost_distribution(df, float_shares=float_shares)
    if not dist:
        return {"profit_ratio": None, "note": "无数据", "proxy": float_shares is None}
    ratio = sum(p for c, p in dist if c < last)
    note = "float_shares 显式给定" if float_shares else CYQ_PROXY_NOTE
    return {"profit_ratio": round(ratio, 4), "note": note,
            "proxy": float_shares is None}


# 指标注册：获利盘比例作为「预计算信号列」反哺因子层（registry docstring 的口径）。
# 输出列 cyq_profit_ratio ∈ [0,1]；需要 float_shares 列（可空）。
@register_indicator("cyq_profit_ratio", label="获利盘比例", category="chip",
                   min_window=5, outputs=("cyq_profit_ratio",))
def add_cyq_profit_ratio(df: pl.DataFrame) -> pl.DataFrame:
    """逐日滚动获利盘比例（对每日收盘价计算当日成本分布的获利占比）。

    为避免 O(n×bins×n) 的全历史重算，滚动实现用「增量分布」：逐日推进同一
    dist 状态，逐日读出获利比例 —— 与 cost_distribution 的末端快照同源。
    """
    if "float_shares" in df.columns:
        fs: float | None = float(df["float_shares"][-1]) or None
    else:
        fs = None
    n = df.height
    px = df.with_columns(_price_col(df).alias("_px"))
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        hi = lo + 1e-9
    bins, width = 60, (hi - lo) / 60
    dist = [0.0] * bins
    ts = _turnover(df, fs).to_list()
    ratios: list[float] = []
    for row, t in zip(px.iter_rows(named=True), ts):
        idx = min(int((row["_px"] - lo) / width), bins - 1)
        for i in range(bins):
            dist[i] *= (1 - t)
        dist[idx] += t
        total = sum(dist) or 1.0
        c = float(row["close"])
        profit = sum(dist[i] / total for i in range(bins)
                     if lo + (i + 0.5) * width < c)
        ratios.append(round(profit, 4))
    return df.with_columns(pl.Series("cyq_profit_ratio", ratios))
