"""把退出策略叠加到任意信号策略上 —— **不改动 Engine**。

``Engine`` 的策略契约是「返回目标权重，非空列表会清掉不在列表里的持仓」
（见 ``Engine._schedule_rebalance`` 的清仓分支）。因此叠加层必须处理一个
容易踩的坑：

    内层策略返回 ``[]``（当日无信号 = 保持持仓）时，若叠加层只为被退出的标的
    返回权重，其余持仓会被引擎当成「不在目标里」**全部清掉**。

正确做法是：一旦有退出信号，就把「当前完整组合」物化出来再打折；
没有退出信号时原样透传 ``[]``，避免无谓换手。

本类因此是纯适配器，新增退出规则不需要动回测引擎一行代码。
"""
from __future__ import annotations

from datetime import date

from lquant.backtest.account import Account
from lquant.backtest.events import Bar
from lquant.backtest.exit.base import ExitContext, ExitStrategy, PositionView
from lquant.backtest.strategy.base import Context, Strategy

__all__ = ["ExitOverlay"]


class ExitOverlay(Strategy):
    """包装内层策略：先取信号，再叠加退出。

    Args:
        inner: 内层信号策略（选股/择时）。
        exit_strategy: 退出策略实例。
        phases: 在每个 bar 上向退出策略声明的阶段标签（默认 ``"close"``）。
            引擎按日收盘决策，所以恒为收盘阶段；保留该字段是为将来
            接入盘中多次决策留出接口。
    """

    def __init__(self, inner: Strategy, exit_strategy: ExitStrategy,
                 phases: str = "close") -> None:
        super().__init__()
        self.inner = inner
        self.exit_strategy = exit_strategy
        self.phases = phases
        self._entry_bar: dict[str, int] = {}
        self._bar_count = 0
        self._account_id: int | None = None

    # ---------- 生命周期 ----------

    def reset(self) -> None:
        """清空全部跨 bar 状态（换账户 / 重跑回测时调用）。"""
        self._entry_bar.clear()
        self._bar_count = 0
        self.exit_strategy.reset()

    def _maybe_reset(self, account: Account) -> None:
        """引擎 ``run()`` 会换一个新 Account 对象；据此自动重置状态。"""
        if self._account_id != id(account):
            self.reset()
            self._account_id = id(account)

    # ---------- 主循环 ----------

    def on_bar(self, ctx: Context, bars: dict[str, Bar]) -> list[tuple[str, float]]:
        self._maybe_reset(ctx.account)
        self._bar_count += 1

        targets = list(self.inner.on_bar(ctx, bars) or [])
        ectx = self._build_exit_context(ctx, bars)
        signals = self.exit_strategy.on_bar(ectx)
        if not signals:
            return targets

        prices = {s: b.close for s, b in bars.items()}
        nav = ctx.account.nav(prices)
        if nav <= 0:
            return targets

        weights = self._materialize(targets, ctx.account, prices, nav)
        exited: set[str] = set()
        for sig in signals:
            cur = weights.get(sig.symbol, 0.0)
            if cur <= 0:
                continue
            new = cur * (1.0 - sig.ratio)
            weights[sig.symbol] = new
            if new <= 0:
                exited.add(sig.symbol)

        # 全量退出必须显式写成 (sym, 0.0)：返回空列表在引擎语义里是
        # 「无操作、保留持仓」，清仓会被静默吞掉。
        out = [(s, w) for s, w in weights.items() if w > 0]
        out.extend((s, 0.0) for s in sorted(exited))
        return out or targets

    # ---------- 内部 ----------

    def _materialize(self, targets: list[tuple[str, float]], account: Account,
                     prices: dict[str, float], nav: float) -> dict[str, float]:
        """把目标权重展开成完整组合；``targets`` 为空时用当前持仓兜底。"""
        weights = {s: w for s, w in targets if w > 0}
        if targets:
            return weights
        for sym, pos in account.positions.items():
            px = prices.get(sym)
            if px and px > 0 and pos.qty > 0:
                weights[sym] = pos.qty * px / nav
        return weights

    def _build_exit_context(self, ctx: Context, bars: dict[str, Bar]) -> ExitContext:
        d: date = ctx.trade_date
        held = {s for s, p in ctx.account.positions.items() if p.qty > 0}
        for sym in list(self._entry_bar):
            if sym not in held:
                del self._entry_bar[sym]

        positions: list[PositionView] = []
        for sym, pos in ctx.account.positions.items():
            if pos.qty <= 0:
                continue
            self._entry_bar.setdefault(sym, self._bar_count)
            rules = ctx.rules.get(sym)
            available = pos.available_at(d, rules) if rules is not None else pos.available_qty
            positions.append(PositionView(
                symbol=sym,
                qty=pos.qty,
                available_qty=available,
                avg_cost=pos.avg_cost,
                holding_days=self._bar_count - self._entry_bar[sym],
            ))
        return ExitContext(trade_date=d, phase=self.phases, positions=positions,
                           bars=bars, rules=ctx.rules)
