"""固定规则退出：硬止损 + 固定止盈 + 最长持股天数。"""
from __future__ import annotations

from lquant.backtest.events import Bar
from lquant.backtest.exit.base import (
    ExitContext,
    ExitSignal,
    ExitStrategy,
    ExitType,
    PositionView,
    is_sealed_limit_down,
)
from lquant.backtest.exit.registry import register_exit_strategy

__all__ = ["SimpleExitStrategy"]


def _breach_ref(bar_open: float, level: float) -> float:
    """盘中破位时的参考成交价：开盘已破按开盘价（跳空），否则按破位价。"""
    return bar_open if bar_open <= level else level


@register_exit_strategy("simple", {"label": "固定止损止盈"})
class SimpleExitStrategy(ExitStrategy):
    """固定比例止损/止盈 + 时间止损。

    参数：
        stop_loss_pct: 硬止损百分比（正数，默认 12.0）。
        take_profit_pct: 固定止盈百分比；``None`` 表示关闭。
        time_stop_days: 最长持股天数；``None`` 表示关闭。
        time_stop_min_return: 时间止损的收益门槛（%，默认 0.0）——
            持股到期但收益高于该值时不强制离场，避免砍掉正在赚钱的仓位。
        stop_mode: ``close``（默认，收盘破位才卖，过滤盘中假摔）
            或 ``intraday``（最低价破位即触发）。
    """

    name = "simple"
    label = "固定止损止盈"

    def __init__(self, stop_loss_pct: float = 12.0,
                 take_profit_pct: float | None = None,
                 time_stop_days: int | None = None,
                 time_stop_min_return: float = 0.0,
                 stop_mode: str = "close",
                 **params: object) -> None:
        super().__init__(**params)
        if stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct 必须为正")
        if stop_mode not in ("close", "intraday"):
            raise ValueError(f"stop_mode 只支持 close/intraday，收到 {stop_mode!r}")
        self.stop_loss_pct = float(stop_loss_pct)
        self.take_profit_pct = take_profit_pct
        self.time_stop_days = time_stop_days
        self.time_stop_min_return = float(time_stop_min_return)
        self.stop_mode = stop_mode

    def on_bar(self, ctx: ExitContext) -> list[ExitSignal]:
        self.track_peak(ctx)
        out: list[ExitSignal] = []
        for pos in ctx.positions:
            if pos.sellable <= 0:
                continue                      # T+N 未解禁，本轮无可卖份额
            bar = ctx.bar(pos.symbol)
            if bar is None or bar.suspended or bar.halted:
                continue

            hit = self._stop_hit(pos, bar)
            if hit is not None:
                if is_sealed_limit_down(bar):
                    continue                  # 一字跌停卖不出去，等打开
                level, ref = hit
                out.append(ExitSignal(pos.symbol, 1.0, ExitType.STOP_LOSS,
                                      f"跌破止损 {level:.2f}", ref))
                continue

            if self._take_profit_hit(pos, bar) is not None:
                out.append(ExitSignal(pos.symbol, 1.0, ExitType.TAKE_PROFIT,
                                      f"达到止盈 {self._take_profit_level(pos):.2f}",
                                      bar.close))
                continue

            if self._time_stop_hit(pos, bar):
                out.append(ExitSignal(pos.symbol, 1.0, ExitType.TIME_STOP,
                                      f"持股 {pos.holding_days} 日未达标", bar.close))
        return out

    # ---------- 判据（子类可复用/覆写） ----------

    def _stop_level(self, pos: PositionView) -> float:
        return pos.avg_cost * (1 - self.stop_loss_pct / 100.0)

    def _stop_hit(self, pos: PositionView, bar: Bar) -> tuple[float, float] | None:
        """返回 (止损价, 参考成交价)；未触发返回 None。"""
        level = self._stop_level(pos)
        if self.stop_mode == "close":
            return (level, bar.close) if bar.close <= level else None
        if bar.low <= level:
            return level, _breach_ref(bar.open, level)
        return None

    def _take_profit_level(self, pos: PositionView) -> float:
        pct = self.take_profit_pct or 0.0
        return pos.avg_cost * (1 + pct / 100.0)

    def _take_profit_hit(self, pos: PositionView, bar: Bar) -> float | None:
        if self.take_profit_pct is None:
            return None
        level = self._take_profit_level(pos)
        return bar.close if bar.close >= level else None

    def _time_stop_hit(self, pos: PositionView, bar: Bar) -> bool:
        return (self.time_stop_days is not None
                and pos.holding_days >= self.time_stop_days
                and pos.pnl_pct(bar.close) < self.time_stop_min_return)
