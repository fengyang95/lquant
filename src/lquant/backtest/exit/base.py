"""退出策略基类、信号模型与股票规则辅助。

**为什么退出要独立成一层**：同一套选股/开仓信号，配不同退出规则会得到完全不同的
绩效曲线。把退出从策略里抽出来，才能做「选股维度 × 退出维度」的正交实验，
而不是给每个策略复制一份止盈止损。

叠加方式见 :class:`lquant.backtest.exit.overlay.ExitOverlay` —— 它包住任意
``Strategy``，在不改动 ``Engine`` 的前提下把退出规则接进去。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from lquant.backtest.events import Bar
from lquant.backtest.rules.model import InstrumentRules

__all__ = [
    "ExitContext",
    "ExitSignal",
    "ExitStrategy",
    "ExitType",
    "PositionView",
    "is_sealed_limit_down",
]


class ExitType(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING = "trailing"
    TIME_STOP = "time_stop"
    CHANNEL = "channel"


@dataclass(frozen=True)
class ExitSignal:
    """一条退出意图。

    ``ratio`` 是**占该持仓的比例**（1.0 = 全部卖出），不是一个组合权重 ——
    分批止盈需要在持仓粒度上表达，权重粒度做不到「卖三分之一」。
    由 :class:`~lquant.backtest.exit.overlay.ExitOverlay` 换算成目标权重。
    """

    symbol: str
    ratio: float
    exit_type: ExitType
    reason: str = ""
    ref_price: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 < self.ratio <= 1.0:
            raise ValueError(f"ratio 必须落在 (0, 1]，收到 {self.ratio}")


@dataclass(frozen=True)
class PositionView:
    """某日的持仓快照（由叠加层从 Account 构造，退出策略只读）。"""

    symbol: str
    qty: float
    available_qty: float      # T+N 约束下当日可卖数量
    avg_cost: float
    holding_days: int = 0     # 首个买入日到今日的交易日跨度（近似：自然日/日线 bar 数）

    @property
    def sellable(self) -> float:
        return min(self.qty, self.available_qty)

    def pnl_pct(self, price: float) -> float:
        """浮动盈亏（百分比刻度，1.0 = +1%）。"""
        if self.avg_cost <= 0:
            return 0.0
        return (price - self.avg_cost) / self.avg_cost * 100.0


@dataclass
class ExitContext:
    """退出决策所需的全部输入。**只包含当日及以前的数据**（防未来函数）。"""

    trade_date: date
    phase: str                                   # open / intraday / close
    positions: list[PositionView]
    bars: dict[str, Bar]
    rules: dict[str, InstrumentRules] = field(default_factory=dict)

    def price(self, symbol: str) -> float | None:
        bar = self.bars.get(symbol)
        return bar.close if bar is not None else None

    def bar(self, symbol: str) -> Bar | None:
        return self.bars.get(symbol)


class ExitStrategy(ABC):
    """退出策略基类。

    子类实现 :meth:`on_bar`，返回**当日**要执行的退出意图列表。
    基类提供两件共用设施：

    - ``self._peak``：逐标的峰值价（移动止盈的基准），由 :meth:`track_peak` 更新；
    - ``self._prev``：逐标的**上一根 bar** 的 ``fields`` 快照，用于表达
      ``REF(x, 1)`` 这类滞后引用（压力位策略需要）。
    """

    name: str = "base"
    label: str = ""

    def __init__(self, **params: object) -> None:
        self.params = params
        self._peak: dict[str, float] = {}
        self._prev: dict[str, dict] = {}
        self._prev_adj: dict[str, float] = {}
        # 本轮 track_peak 感知到的除权比 {sym: f_t/f_{t-1}}（每轮清空）。
        # 自存「绝对价格状态」的子类（如 tiered 的棘轮止盈线）必须用它
        # 同步缩放，否则除权日后状态停在除权前价格尺度 —— peak/avg_cost
        # 缩了、棘轮没缩，止盈线被抬到现价之上，除权次日必然假退出。
        self._turn_ratios: dict[str, float] = {}

    # ---------- 子类实现 ----------

    @abstractmethod
    def on_bar(self, ctx: ExitContext) -> list[ExitSignal]:
        """返回当日退出信号；空列表 = 不退出。"""

    # ---------- 共用设施 ----------

    def reset(self) -> None:
        """清空跨 bar 状态。引擎重复 run 或复用到新账户时必须调用。"""
        self._peak.clear()
        self._prev.clear()
        self._prev_adj.clear()
        self._turn_ratios.clear()

    def track_peak(self, ctx: ExitContext) -> None:
        """用当日最高价更新峰值；持仓已清则丢弃该标的的峰值。

        用 ``high`` 而非 ``close`` 作为峰值基准：最高价在收盘时已经发生过，
        不属于未来数据；而移动止盈要防的正是「冲高回落」。

        除权日缩放用**相邻两日的因子比**（f_t / f_{t-1}），但方向必须与账户层
        份额调整一致：``qty *= ratio`` 且 **``avg_cost /= ratio``** —— peak 与
        avg_cost 同为每股原始价口径，所以历史峰值同样 ``/= ratio``（10 送 5：
        ratio=1.5，旧峰值 10 元 ÷1.5 = 6.67，与新除权价 6.67 持平 —— 除权本身
        不产生盈亏）。此前误写 ``prev * ratio``，方向与 avg_cost 相反：除权日
        峰值被放大而成本被缩小，假浮盈把止盈线抬到现价之上，移动止盈在每次
        除权后立即误触发。更早版本直接乘绝对累计因子（后复权因子锚定上市日、
        单调增长），峰值逐日指数爆炸，问题同源。
        """
        held = {p.symbol for p in ctx.positions if p.qty > 0}
        self._turn_ratios.clear()
        for sym in list(self._peak):
            if sym not in held:
                del self._peak[sym]
                self._prev_adj.pop(sym, None)
        for sym in held:
            bar = ctx.bars.get(sym)
            if bar is None:
                continue
            f = bar.adj_factor if bar.adj_factor > 0 else 1.0
            prev = self._peak.get(sym, 0.0)
            if prev <= 0:
                base = bar.high                      # 首日：峰值就是当日 high
            else:
                prev_f = self._prev_adj.get(sym)
                ratio = f / prev_f if prev_f and prev_f > 0 else 1.0
                base = prev / ratio                  # 除权日按因子比缩放旧峰值（与 avg_cost 同向）
                if abs(ratio - 1.0) > 1e-12:
                    self._turn_ratios[sym] = ratio   # 暴露给子类同步缩放自有价格状态
            self._peak[sym] = max(base, bar.high)
            self._prev_adj[sym] = f

    def peak_of(self, position: PositionView) -> float:
        """该标的峰值价；无记录时回退到成本价。"""
        return self._peak.get(position.symbol) or position.avg_cost

    def ref_prev(self, ctx: ExitContext, symbol: str, key: str) -> float | None:
        """``REF(key, 1)``：上一根 bar 的 ``fields[key]``。"""
        return self._prev.get(symbol, {}).get(key)

    def snapshot_fields(self, ctx: ExitContext) -> None:
        """把当日 ``fields`` 存起来，供下一根 bar 做 ``REF``。"""
        for sym, bar in ctx.bars.items():
            if bar.fields:
                self._prev[sym] = dict(bar.fields)

    def describe(self) -> dict:
        """自省：给前端列出可用退出策略与其参数。"""
        return {"name": self.name, "label": self.label or self.name, "params": self.params}


def is_sealed_limit_down(bar: Bar, tol: float = 1e-9) -> bool:
    """一字跌停（封死）：**当日无法卖出**。

    判据 = 一字板（最高 = 最低）且相对昨收下跌。
    若不排除这种情况，「收盘价跌破止损」会生成一张注定无法成交的卖单，
    回测要么静默拒单、要么按不可能的价格成交从而虚增平仓收益。
    一字**涨停**不在此列 —— 涨停板上有买盘，是能卖出去的。
    """
    if bar.pre_close <= 0 or bar.high <= 0:
        return False
    flat = abs(bar.high - bar.low) <= tol * max(1.0, abs(bar.high))
    return flat and bar.close < bar.pre_close - tol * max(1.0, abs(bar.pre_close))
