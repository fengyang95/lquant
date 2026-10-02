"""分级移动止盈 + 硬止损 + 时间止损。

移植自 FinancialTool 的 ``TieredExitStrategy``，并修正其两处缺陷：

1. **原实现在 ``open`` 阶段不做任何检查**，跳空低开当天不执行止损，
   等到收盘才卖 —— 等于把跳空缺口全额吃下。本实现在每个 bar 都检查。
2. **原实现无条件在跌停日卖出**，回测里会以不可能的价格成交。本实现用
   :func:`~lquant.backtest.exit.base.is_sealed_limit_down` 挡掉一字跌停。

分级规则（原实现的阈值来自实盘调参，保留）：按**峰值曾达到的最大盈利**决定
可容忍的回撤：

    max_gain ≥ 50% → 容忍回撤 5%
    max_gain ≥ 30% → 容忍回撤 8%
    其余          → 容忍回撤 10%

止盈线 = ``峰值 × (1 − 容忍回撤)``，且**只上移不下移**（棘轮）。盈利越多、
容忍度越小 —— 这是「让利润奔跑，但回吐到一定程度就走」的可执行表达。
"""
from __future__ import annotations

from lquant.backtest.exit.base import (
    ExitContext,
    ExitSignal,
    ExitType,
    PositionView,
    is_sealed_limit_down,
)
from lquant.backtest.exit.registry import register_exit_strategy
from lquant.backtest.exit.simple import SimpleExitStrategy

__all__ = ["TieredExitStrategy", "DEFAULT_TIERS"]

#: (峰值盈利下限 %, 容忍回撤 %) —— 从高到低匹配
DEFAULT_TIERS: tuple[tuple[float, float], ...] = ((50.0, 5.0), (30.0, 8.0))
#: 未达到任何档位时的基础容忍回撤
DEFAULT_BASE_DRAWDOWN = 10.0


@register_exit_strategy("tiered", {"label": "分级移动止盈"})
class TieredExitStrategy(SimpleExitStrategy):
    """动态分级止盈（在 :class:`SimpleExitStrategy` 之上叠加移动止盈）。

    参数继承 ``simple``，另加：
        tiers: 分级档位，默认 :data:`DEFAULT_TIERS`。
        base_drawdown_pct: 基础容忍回撤，默认 10.0。
        trailing_floor_pct: 移动止盈的启用门槛（峰值盈利低于此值不启用），
            默认 0.0 —— 即建仓后立刻开始棘轮。
    """

    name = "tiered"
    label = "分级移动止盈"

    def __init__(self,
                 tiers: tuple[tuple[float, float], ...] = DEFAULT_TIERS,
                 base_drawdown_pct: float = DEFAULT_BASE_DRAWDOWN,
                 trailing_floor_pct: float = 0.0,
                 stop_loss_pct: float = 12.0,
                 time_stop_days: int | None = 20,
                 time_stop_min_return: float = 5.0,
                 take_profit_pct: float | None = None,
                 stop_mode: str = "close",
                 **params: object) -> None:
        super().__init__(stop_loss_pct=stop_loss_pct,
                         take_profit_pct=take_profit_pct,
                         time_stop_days=time_stop_days,
                         time_stop_min_return=time_stop_min_return,
                         stop_mode=stop_mode,
                         **params)
        ordered = tuple(sorted(tiers, key=lambda t: -t[0]))
        if any(dd <= 0 for _, dd in ordered):
            raise ValueError("容忍回撤必须为正")
        if base_drawdown_pct <= 0:
            raise ValueError("base_drawdown_pct 必须为正")
        self.tiers = ordered
        self.base_drawdown_pct = float(base_drawdown_pct)
        self.trailing_floor_pct = float(trailing_floor_pct)
        self._trailing_stop: dict[str, float] = {}

    def reset(self) -> None:
        super().reset()
        self._trailing_stop.clear()

    # ---------- 分级 ----------

    def drawdown_tolerance(self, max_gain_pct: float) -> float:
        """按峰值盈利取容忍回撤（%）。"""
        for floor, dd in self.tiers:
            if max_gain_pct >= floor:
                return dd
        return self.base_drawdown_pct

    def trailing_stop_level(self, pos: PositionView, peak: float) -> float:
        """当前止盈线；**棘轮**：只上移不下移。"""
        gain = (peak - pos.avg_cost) / pos.avg_cost * 100.0 if pos.avg_cost > 0 else 0.0
        if gain < self.trailing_floor_pct:
            return 0.0
        level = peak * (1 - self.drawdown_tolerance(gain) / 100.0)
        prev = self._trailing_stop.get(pos.symbol, 0.0)
        level = max(level, prev)
        self._trailing_stop[pos.symbol] = level
        return level

    def on_bar(self, ctx: ExitContext) -> list[ExitSignal]:
        self.track_peak(ctx)
        held = {p.symbol for p in ctx.positions if p.qty > 0}
        for sym in list(self._trailing_stop):
            if sym not in held:
                del self._trailing_stop[sym]

        out: list[ExitSignal] = []
        for pos in ctx.positions:
            if pos.sellable <= 0:
                continue
            bar = ctx.bar(pos.symbol)
            if bar is None or bar.suspended or bar.halted:
                continue

            # 优先级 1：硬止损（跳空低开同样命中，修正原实现 open 阶段空转）
            hit = self._stop_hit(pos, bar)
            if hit is not None:
                if is_sealed_limit_down(bar):
                    continue
                level, ref = hit
                out.append(ExitSignal(pos.symbol, 1.0, ExitType.STOP_LOSS,
                                      f"跌破止损 {level:.2f}", ref))
                continue

            # 优先级 2：分级移动止盈
            level = self.trailing_stop_level(pos, self.peak_of(pos))
            if level > 0 and bar.close <= level:
                out.append(ExitSignal(pos.symbol, 1.0, ExitType.TRAILING,
                                      f"回撤触发移动止盈 {level:.2f}", bar.close))
                continue

            # 优先级 3：时间止损
            if self._time_stop_hit(pos, bar):
                out.append(ExitSignal(pos.symbol, 1.0, ExitType.TIME_STOP,
                                      f"持股 {pos.holding_days} 日未达标", bar.close))
        return out
