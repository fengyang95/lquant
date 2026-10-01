"""通道压力位分批止盈（在分级移动止盈之上叠加）。

移植自 FinancialTool 的 ``TiandaoPressureExitStrategy``：以天道通道**上一根的
金牛线**为压力位，分批止盈而不是一次性清仓 —— 因为压力位被突破后继续上行的
概率并不低，一次清光会系统性错失主升段。

执行分级：

    A 档：首次 ``high ≥ 压力位``  → 卖 ``ratio_a``（默认 1/3）
    B 档：A 档之后 ``close < 压力位`` → 再卖剩余 ``ratio_b``（默认 1/2）
    C 档：移动止盈/止损触发       → 全部清掉

优先级：**C（止损/移动止盈）> A/B > 时间止损**。

压力位取 ``REF(金牛, 1)`` 而不是当日金牛 —— 当日金牛包含当日最高价，
用它做「触及压力」判定等于同时用结果和原因。
本实现通过 :meth:`ExitStrategy.ref_prev` 读取上一根 bar 的指标快照，
调用方需要把指标算进 ``Bar.fields``（见 ``tests/unit/test_exit_overlay.py``）。
"""
from __future__ import annotations

from lquant.backtest.exit.base import (
    ExitContext,
    ExitSignal,
    ExitType,
    PositionView,
)
from lquant.backtest.exit.registry import register_exit_strategy
from lquant.backtest.exit.tiered import TieredExitStrategy

__all__ = ["PressureExitStrategy", "DEFAULT_PRESSURE_FIELD"]

DEFAULT_PRESSURE_FIELD = "td_jinniu"


@register_exit_strategy("pressure", {"label": "通道压力位分批止盈"})
class PressureExitStrategy(TieredExitStrategy):
    """压力位分批止盈。

    参数继承 ``tiered``，另加：
        pressure_field: ``Bar.fields`` 中的压力位列名，默认 ``td_jinniu``。
        ratio_a: A 档卖出比例（默认 1/3）。
        ratio_b: B 档卖出比例（默认 1/2，指当时剩余持仓的比例）。
    """

    name = "pressure"
    label = "通道压力位分批止盈"

    def __init__(self, pressure_field: str = DEFAULT_PRESSURE_FIELD,
                 ratio_a: float = 1.0 / 3.0, ratio_b: float = 0.5,
                 **params: object) -> None:
        super().__init__(**params)
        for label, r in (("ratio_a", ratio_a), ("ratio_b", ratio_b)):
            if not 0.0 < r <= 1.0:
                raise ValueError(f"{label} 必须落在 (0, 1]")
        self.pressure_field = pressure_field
        self.ratio_a = float(ratio_a)
        self.ratio_b = float(ratio_b)
        self._stage: dict[str, int] = {}

    def reset(self) -> None:
        super().reset()
        self._stage.clear()

    def on_bar(self, ctx: ExitContext) -> list[ExitSignal]:
        # 先让父类算 C 档（止损 / 移动止盈 / 时间止损）
        tiered_hits = super().on_bar(ctx)
        by_symbol = {s.symbol: s for s in tiered_hits}

        # A/B 档只对「没被 C 档命中」的持仓生效
        out: list[ExitSignal] = []
        for pos in ctx.positions:
            if pos.sellable <= 0:
                continue
            bar = ctx.bar(pos.symbol)
            if bar is None or bar.suspended or bar.halted:
                continue
            hard = by_symbol.get(pos.symbol)
            if hard is not None:
                out.append(hard)
                continue
            sig = self._channel_step(ctx, pos, bar)
            if sig is not None:
                out.append(sig)

        # 父类可能对已不在 ctx.positions 的标的报信号（不应发生），保守起见去掉
        known = {p.symbol for p in ctx.positions}
        out = [s for s in out if s.symbol in known]
        self.snapshot_fields(ctx)
        return out

    def _channel_step(self, ctx: ExitContext, pos: PositionView,
                      bar) -> ExitSignal | None:
        pressure = self.ref_prev(ctx, pos.symbol, self.pressure_field)
        if pressure is None or pressure <= 0:
            return None
        stage = self._stage.get(pos.symbol, 0)

        if stage == 0 and bar.high >= pressure:
            self._stage[pos.symbol] = 1
            return ExitSignal(pos.symbol, self.ratio_a, ExitType.CHANNEL,
                              f"触及压力位 {pressure:.2f}（A 档）", pressure)

        if stage == 1 and bar.close < pressure:
            self._stage[pos.symbol] = 2
            return ExitSignal(pos.symbol, self.ratio_b, ExitType.CHANNEL,
                              f"回落压力位下方 {pressure:.2f}（B 档）", bar.close)
        return None

    def track_peak(self, ctx: ExitContext) -> None:
        super().track_peak(ctx)
        held = {p.symbol for p in ctx.positions if p.qty > 0}
        for sym in list(self._stage):
            if sym not in held:
                del self._stage[sym]
