"""筹码分布 CYQ（Chip/筹码，业内通称）：成本分布与获利盘比例。

借鉴 InStock（14.7k star）``kline/cyq.py`` 的思路，按 lquant 风格重写：

**换手率衰减模型**（CYQ 的通行算法）：
    每个交易日，历史筹码按当日换手率 ``t = volume / float_shares`` 被部分换手
    —— 旧筹码留存 ``(1 - t)``，当日新筹码 ``t`` 落在成交代表价附近。
    迭代到底得到「按价格分布的持仓成本」：
        dist = dist * (1 - t) + t * day_bin

**float_shares 的两种口径**（按优先级，evidence 不静默）：
    1. 调用方显式传入（股数）—— 最准；
    2. 缺省代理：当日量 / 截至前一日的**累积均量**（expanding、不含当日）。
       A 股数据源普遍不直接给流通股本；「不含当日」仿量比指标的防自稀释
       （放量日若把分母一起抬高会自己稀释自己），且严格因果 —— 这是滚动
       指标通过前缀不变性门禁的前提。代理口径会显式标注（``CYQ_PROXY_NOTE``）。

**无网格解析式 vs 直方图**（为什么有两套实现）：
    - ``profit_ratio`` / 注册指标 ``cyq_profit_ratio`` 用**解析式滚动**：
      每日筹码视作单点（成交代表价 ``(H+L+2C)/4``），权重随衰减累积，
      获利比例 = Σ(成本 < 现价的权重) / Σ(总权重)。无全局网格 →
      每日值只依赖前缀数据，严格满足 ``assert_no_lookahead`` 门禁。
    - ``cost_distribution`` 用**直方图**（bins 等宽价格格），网格边界依赖
      全量数据的 min/max —— 前缀重算会漂移，**只用于末端快照可视化**，
      不进滚动序列、不参与回测信号。

输出：
    - ``cost_distribution(df, bins)``：末端成本分布（bin 中心 → 概率和 1）
    - ``profit_ratio(df)``：末端获利盘比例（0~1），evidence 带口径标注
    - 注册指标 ``cyq_profit_ratio``：逐日滚动获利盘比例列
"""

from __future__ import annotations

import numpy as np
import polars as pl

from lquant.indicators.registry import register_indicator

__all__ = ["cost_distribution", "profit_ratio", "CYQ_PROXY_NOTE"]

CYQ_PROXY_NOTE = "float_shares 未给定：换手率用量/累积均量代理（相对结构正确，非精确口径）"

# CYQ 依赖的输入列。注册指标的 inputs 与缺列防御共用这一份清单。
_REQUIRED = ("high", "low", "close", "volume")


def _price(df: pl.DataFrame) -> pl.Series:
    """当日成交代表价：经典 CYQ 用 ``(H+L+2C)/4`` —— 比单 close 更贴真实成交重心。"""
    return ((df["high"] + df["low"] + 2 * df["close"]) / 4).rename("_px")


def _turnover(df: pl.DataFrame, float_shares) -> list[float]:
    """每日换手率 t ∈ [0, 1]。

    ``float_shares`` 三档口径：

    - ``pl.Series``（逐日股本列）：``t_i = volume_i / float_shares_i`` —— 逐行
      取值，送转/增发当日之后的换手率随之变化。**必须逐行**：把整列折成一个
      标量会让早期行的 t 依赖后面才出现的股本，前缀重算就变（未来函数）。
      个别日 null/0 时该行退回代理口径，而不是整段跳成 0。
    - 正数标量（调用方按日传入的常数）：``t = volume / float_shares``。
    - ``None``：代理口径 ``t = volume / 截至前一日累积均量``（expanding、不含当日）。

    代理口径的首日（或此前累计无成交）取 1.0：首日之前没有历史筹码，全部筹码
    落在当日，这是模型的初始条件而不是异常降级；当日本身无成交则 t=0（不产生
    新筹码），连续无成交会让总权重归零，上层显式输出 None 而不是伪造数值。
    """
    v = df["volume"].to_list()
    ts = _proxy_turnover(v)
    if isinstance(float_shares, pl.Series):
        fs = float_shares.to_list()
        if len(fs) != len(v):
            raise ValueError(f"float_shares 长度 {len(fs)} 与面板 {len(v)} 不一致")
        return [
            min(vi / fi, 1.0) if (fi is not None and fi > 0) else ts[i]
            for i, (vi, fi) in enumerate(zip(v, fs, strict=True))
        ]
    if float_shares and float_shares > 0:
        return [min(vi / float_shares, 1.0) for vi in v]
    return ts


def _proxy_turnover(v: list[float]) -> list[float]:
    """代理口径换手率：``t_i = volume_i / 截至前一日的累积均量``（严格因果）。"""
    ts: list[float] = []
    cum = 0.0
    for i, vi in enumerate(v):
        if vi <= 0:
            ts.append(0.0)  # 当日无成交：无新筹码
        elif i == 0 or cum <= 0:
            ts.append(1.0)  # 首日/此前累计无成交：全部筹码落在当日
        else:
            # expanding 均量（不含当日）：严格因果，前缀重算不变
            ts.append(min(vi / (cum / i), 1.0))
        cum += vi
    return ts


def _rolling_ratios(df: pl.DataFrame, float_shares) -> list[float | None]:
    """逐日获利盘比例（解析式滚动，严格前缀不变）。

    第 i 日新筹码 ``t_i`` 落在 ``p_i``，此后每过一天留存 ``(1 - t_j)``。
    终点 t 的获利比例 = Σ_{i≤t, p_i < close_t} w_i / Σ_{i≤t} w_i。

    实现为预分配数组逐日推进：每日 O(size)，总 O(n²) 但全是 NumPy 向量操作
    （n=250 时约 6 万次元素级运算，微秒级）；代价换正确性 —— 排序前缀和的
    O(n log n) 写法会把 t 之后的筹码算进 t 时刻，那才是真未来函数。
    """
    n = df.height
    px = _price(df).to_list()
    closes = df["close"].to_list()
    ts = _turnover(df, float_shares)
    ps = np.empty(n)
    ws = np.empty(n)
    out: list[float | None] = []
    size = 0
    for i in range(n):
        if size:
            ws[:size] *= 1.0 - ts[i]
        ps[size] = px[i]
        ws[size] = ts[i]
        size += 1
        total = float(ws[:size].sum())
        if total <= 0:
            # 历史清零且当日无成交：无筹码信息，显式 None，不伪造 0 或 1
            out.append(None)
            continue
        profit = float(ws[:size][ps[:size] < closes[i]].sum())
        out.append(round(profit / total, 4))
    return out


def cost_distribution(
    df: pl.DataFrame, bins: int = 60, float_shares: float | None = None
) -> list[tuple[float, float]]:
    """末端成本分布。返回 [(bin 中心价, 概率)]，概率和为 1。

    **仅用于末端快照可视化**：网格边界取全量数据的 min(low)..max(high)，
    换一段前缀网格就变 —— 不满足前缀不变性，绝不能把它的逐日值当信号列。

    迭代：每根 K 线后 ``dist = dist*(1-t) + t*day_bin``。bins 是常数量级
    （默认 60），纯 Python 循环足够，性能瓶颈不在这里。
    """
    if df.is_empty():
        return []
    px = df.with_columns(_price(df))
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        hi = lo + 1e-9
    width = (hi - lo) / bins
    dist = [0.0] * bins
    ts = _turnover(df, float_shares)

    for row, t in zip(px.iter_rows(named=True), ts, strict=True):
        p = row["_px"]
        idx = min(int((p - lo) / width), bins - 1)
        for i in range(bins):
            dist[i] *= 1 - t
        dist[idx] += t
    total = sum(dist) or 1.0
    return [(lo + (i + 0.5) * width, dist[i] / total) for i in range(bins) if dist[i] > 0]


def profit_ratio(df: pl.DataFrame, float_shares: float | None = None) -> dict:
    """末端获利盘比例：成本低于现价的筹码占比（0~1）。>0.9 常被视为「高位获利盘重」。

    与滚动指标 ``cyq_profit_ratio`` 同一解析式实现（取末端值），两者口径一致；
    evidence 里诚实标注 float_shares 是显式口径还是代理口径。
    """
    if df.is_empty() or "close" not in df.columns:
        return {"profit_ratio": None, "note": "无数据", "proxy": float_shares is None}
    ratios = _rolling_ratios(df, float_shares)
    note = "float_shares 显式给定" if float_shares else CYQ_PROXY_NOTE
    return {
        "profit_ratio": ratios[-1] if ratios else None,
        "note": note,
        "proxy": float_shares is None,
    }


# 指标注册：获利盘比例作为「预计算信号列」反哺因子层（registry docstring 的口径）。
# 输出 cyq_profit_ratio ∈ [0,1]；category 归 oscillator（0~1 比率画子图，与 RSI 同类），
# registry.CATEGORIES 没有 "chip" 类别，不为此单开类别污染前端枚举。
@register_indicator(
    "cyq_profit_ratio",
    label="获利盘比例",
    category="oscillator",
    min_window=5,
    inputs=_REQUIRED,
    outputs=("cyq_profit_ratio",),
)
def add_cyq_profit_ratio(df: pl.DataFrame) -> pl.DataFrame:
    """逐日滚动获利盘比例（0~1）。解析式实现，严格前缀不变（无未来函数）。

    缺输入列时输出全 null 占位（保持行数）—— outputs 声明了这列，下游
    （前端/批量链）期望它存在，所以补 null 列而不是原样返回；
    与 ``add_turnover_ma`` 的「缺列 noop」同一防御哲学，取更保守的一档。

    float_shares 口径：**逐行** ``t_i = volume_i / ffill(float_shares)_i`` ——
    送转/增发当日之后的换手率随股本变化，早期行只看得到当期及此前的股本
    （前缀不变性因此成立）。此前把整列折成「最后一个非空值」贯穿全历史，
    等于让早期行读到了未来才知道的股本，既失真又破坏前缀不变性门禁。
    股本列缺失（主路径 ``read_daily`` 就没有这列）或该行 null/0 时，退回
    「volume / 截至前一日累积均量」的代理口径（逐行兜底，不整段跳 0）。
    """
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("cyq_profit_ratio"))
    fs: float | pl.Series | None = None
    if "float_shares" in df.columns:
        col = df["float_shares"].cast(pl.Float64, strict=False).forward_fill()
        if col.drop_nulls().len() > 0:
            fs = col
    ratios = _rolling_ratios(df, fs)
    return df.with_columns(pl.Series("cyq_profit_ratio", ratios, dtype=pl.Float64))
