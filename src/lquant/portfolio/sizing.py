"""单标的仓位模型：ATR 风险预算 + Kelly 公式（借鉴 abu 的仓位管理思想）。

abu（12k star）把仓位管理抽象为可插拔的 ``AbuPositionBase``（ATR 仓位是
其默认实现）。lquant 的分层里，**仓位模型与组合权重互补**：
``portfolio/weighting.py`` 解决「一篮子怎么分」（横截面），
本模块解决「单标的下多少」（时间序列，逐日可用）。

两个模型共用同一契约：
  - 返回目标权重 ∈ [0, max_weight]，**绝不返回负数**（做空不在选股链路）；
  - 输入异常（ATR≤0、参数越界）显式报错，不静默夹逼出错误数字；
  - 无优势/无信息 → 显式 0 仓位，这是决策不是缺失值。

**为什么 ATR 仓位是「风险预算」**：权重 ∝ 单日风险预算 / 单日实际波动
（``daily_risk * close / ATR``）。波动大的标的自动少配，使每笔交易的
单日盈亏对组合的冲击大致恒定 —— 与 ``backtest/benchmarks.py`` 的海龟
benchmark 同公式，但那里是回测内嵌实现，这里是可复用的独立函数。

**Kelly 折扣**：全 Kelly 波动大到心理上不可执行，实务普遍打半仓
（``fraction=0.5`` 默认）。期望劣势时 f* ≤ 0 → 直接给 0 并由调用方
看 note 判断，而不是返回负权重。
"""

from __future__ import annotations

import math

__all__ = ["atr_weight", "kelly_fraction"]


def atr_weight(
    close: float, atr: float, *, daily_risk: float = 0.01, max_weight: float = 1.0
) -> float:
    """ATR 风险预算仓位：``daily_risk * close / ATR``，封顶 max_weight。

    Args:
        close: 现价（元）。lquant 硬约束：价格单位元。
        atr: 平均真实波幅（同单位）。
        daily_risk: 单日风险预算占比（默认 1%：ATR 一个波幅 ≈ 净值的 1%）。
        max_weight: 权重上限。

    ATR ≤ 0 或 close ≤ 0 是数据错误而非「零仓位」，显式报错 ——
    静默夹逼会把脏数据伪装成保守决策。NaN 的比较恒为 False，所以这里必须
    用 ``math.isfinite`` 而不是 ``<= 0``：否则 NaN 会穿透守卫，再经
    ``min(raw, max_weight)`` 原样返回 NaN 权重。
    """
    if not math.isfinite(close) or close <= 0:
        raise ValueError(f"close 必须为有限正数，得到 {close!r}")
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError(f"ATR 必须为有限正数（数据缺失请上层显式降级），得到 {atr!r}")
    if not math.isfinite(daily_risk) or daily_risk <= 0:
        raise ValueError(f"daily_risk 必须为有限正数，得到 {daily_risk!r}")
    # 契约：返回 ∈ [0, max_weight] 且绝不为负（做空不在选股链路）。
    # max_weight ≤ 0 会让 min(raw, max_weight) 直接吐出负权重 —— 显式拒绝。
    if not math.isfinite(max_weight) or not 0.0 < max_weight <= 1.0:
        raise ValueError(f"max_weight 必须在 (0,1]，得到 {max_weight!r}")
    raw = daily_risk * close / atr
    return min(raw, max_weight)


def kelly_fraction(win_rate: float, win_loss_ratio: float, *, fraction: float = 0.5) -> float:
    """Kelly 公式（打 ``fraction`` 折）：f* = fraction × (p − (1−p)/b)。

    Args:
        win_rate: 胜率 p ∈ (0, 1)。
        win_loss_ratio: 盈亏比 b = 平均盈利 / 平均亏损 > 0。
        fraction: Kelly 折扣（默认 0.5 = half-Kelly；1 = 全 Kelly）。

    期望劣势（p ≤ 1/(1+b)，即 f* ≤ 0）返回 0.0 —— 「不下注」是决策；
    参数越界报错（用错误的胜率算仓位比不用更危险）。
    """
    if not 0.0 < win_rate < 1.0:
        raise ValueError(f"win_rate 必须在 (0,1) 开区间，得到 {win_rate!r}")
    if win_loss_ratio <= 0:
        raise ValueError(f"win_loss_ratio 必须为正，得到 {win_loss_ratio!r}")
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction 必须在 (0,1]，得到 {fraction!r}")
    full = win_rate - (1.0 - win_rate) / win_loss_ratio
    if full <= 0.0 or not math.isfinite(full):
        return 0.0
    return min(full * fraction, 1.0)
