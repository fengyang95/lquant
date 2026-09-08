"""滑点模型：pct / tick / volume_pct / 冲击成本。

默认用 pct（按成交额比例）而不是固定点数 —— 因为 A 股股价从 2 元到 2000 元，
固定点数滑点对高价股几乎无影响、对低价股却能吃掉全部收益。

滑点必须区分方向：买往上滑、卖往下滑。写成同一个符号等于负滑点，
回测会出现「越交易越赚」的荒谬结果。
"""
from __future__ import annotations

from dataclasses import dataclass

from lquant.backtest.events import Side

__all__ = ["PctSlippage", "TickSlippage", "VolumePctSlippage", "NoSlippage",
           "make_slippage", "SLIPPAGE"]


@dataclass
class PctSlippage:
    """按成交价固定比例（如 0.0005 = 万五）。"""

    rate: float = 0.0005

    def apply(self, price: float, side: Side) -> float:
        return price * (1 + self.rate) if side == Side.BUY else price * (1 - self.rate)


@dataclass
class TickSlippage:
    """按最小变动价位跳档。A 股 tick = 0.01 元。"""

    tick: float = 0.01
    n: int = 1

    def apply(self, price: float, side: Side) -> float:
        d = self.tick * self.n
        return price + d if side == Side.BUY else max(price - d, self.tick)


@dataclass
class VolumePctSlippage:
    """成交量冲击：按委托量占当日成交量的比例放大滑点。

    小市值票的下单量一旦占到日成交量几个点，冲击成本会急剧上升 ——
    用固定 pct 会系统性低估这类成本，导致小票策略回测虚高。
    """

    participation_cap: float = 0.05      # 委托量最多占当日成交量的比例
    impact: float = 0.1                  # 冲击系数
    base_rate: float = 0.0002

    def apply(self, price: float, side: Side, qty: float = 0.0, volume: float = 0.0) -> float:
        rate = self.base_rate
        if volume > 0 and qty > 0:
            part = min(qty / volume, self.participation_cap)
            rate += self.impact * (part ** 2)      # 平方根模型，冲击随占比超线性
        return price * (1 + rate) if side == Side.BUY else price * (1 - rate)


@dataclass
class NoSlippage:
    """零滑点。只用于和真实滑点做对照，看成本到底吃掉多少。"""

    def apply(self, price: float, side: Side) -> float:
        return price


SLIPPAGE = {
    "pct": PctSlippage,
    "tick": TickSlippage,
    "volume_pct": VolumePctSlippage,
    "none": NoSlippage,
}


def make_slippage(kind: str = "pct", **kw):
    """从配置名构造滑点模型。"""
    if kind not in SLIPPAGE:
        raise KeyError(f"未知滑点模型 {kind!r}，可选: {sorted(SLIPPAGE)}")
    return SLIPPAGE[kind](**kw)
