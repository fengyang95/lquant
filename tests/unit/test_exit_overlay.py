"""退出叠加层：与既有 ``Engine`` 的契约对齐（不改引擎一行代码）。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.events import Bar
from lquant.backtest.exit import (
    ExitContext,
    ExitOverlay,
    ExitSignal,
    ExitStrategy,
    ExitType,
    SimpleExitStrategy,
    TieredExitStrategy,
    get_exit_strategy,
)
from lquant.backtest.strategy.base import Strategy

SYM = "600519.SH"


# ---------- 夹具 ----------

class BuyOnce(Strategy):
    """首日买入 90%，之后保持不动（返回 [] = 无操作）。"""

    def __init__(self, weight: float = 0.9) -> None:
        super().__init__()
        self.weight = weight

    def on_bar(self, ctx, bars):
        held = sum(1 for p in ctx.account.positions.values() if p.qty > 0)
        return [] if held else [(SYM, self.weight)]


class RecordingExit(ExitStrategy):
    """按预设脚本发信号，用于精确验证叠加层的权重换算。"""

    name = "recording"

    def __init__(self, script: dict[int, float] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.script = script or {}
        self.calls: list[ExitContext] = []
        self._bar = 0

    def on_bar(self, ctx: ExitContext) -> list[ExitSignal]:
        self._bar += 1
        self.calls.append(ctx)
        ratio = self.script.get(self._bar)
        return [] if ratio is None else [ExitSignal(SYM, ratio, ExitType.TRAILING, "脚本")]


def make_data(prices: list[float]) -> dict[date, dict[str, Bar]]:
    d0 = date(2026, 1, 5)
    out: dict[date, dict[str, Bar]] = {}
    for i, p in enumerate(prices):
        d = d0 + timedelta(days=i)
        prev = prices[i - 1] if i else p
        out[d] = {SYM: Bar(symbol=SYM, trade_date=d, open=prev,
                           high=max(prev, p), low=min(prev, p), close=p,
                           pre_close=prev, volume=1e9, amount=1e9)}
    return out


# ---------- 叠加层契约 ----------

def test_no_signal_passes_targets_through():
    ov = ExitOverlay(BuyOnce(), RecordingExit())
    data = make_data([100.0] * 4)
    res = Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(data)
    assert any(f.side.value == "buy" for f in res.trades)
    assert not any(f.side.value == "sell" for f in res.trades)


def test_full_exit_actually_sells():
    """回归：全量退出若被写成「从权重里删掉」，返回空列表会被引擎当成无操作。"""
    ov = ExitOverlay(BuyOnce(), RecordingExit(script={3: 1.0}))
    res = Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(make_data([100.0] * 6))
    sells = [f for f in res.trades if f.side.value == "sell"]
    assert sells, "全量退出没有产生卖单 —— 叠加层又退化成「空列表 = 无操作」了"
    assert sum(f.qty for f in sells) == pytest.approx(9000.0)


def test_partial_exit_reduces_weight():
    ov = ExitOverlay(BuyOnce(), RecordingExit(script={3: 0.5}))
    res = Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(make_data([100.0] * 6))
    sells = [f for f in res.trades if f.side.value == "sell"]
    assert sells and sum(f.qty for f in sells) == pytest.approx(4500.0, rel=1e-3)


def test_inner_empty_targets_materializes_portfolio():
    """内层返回 [] 时，退出仍然生效且不会顺带清掉其它标的。"""
    rec = RecordingExit(script={3: 1.0})
    ov = ExitOverlay(BuyOnce(), rec)
    Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(make_data([100.0] * 6))
    last = rec.calls[-1]
    assert last.positions, "退出上下文应当包含当前持仓"


def test_holding_days_counts_bars():
    rec = RecordingExit()
    ov = ExitOverlay(BuyOnce(), rec)
    Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(make_data([100.0] * 6))
    days = [c.positions[0].holding_days for c in rec.calls if c.positions]
    assert days == sorted(days) and days[-1] >= 4


def test_state_resets_when_account_changes():
    rec = RecordingExit()
    ov = ExitOverlay(BuyOnce(), rec)
    data = make_data([100.0] * 5)
    Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(data)
    first = len(rec.calls)
    rec.calls.clear()
    Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(data)
    assert len(rec.calls) == first, "第二次 run 应从干净状态开始"


def test_overlay_exposes_describe_and_reset():
    ov = ExitOverlay(BuyOnce(), RecordingExit(a=1))
    assert ov.exit_strategy.describe()["name"] == "recording"
    ov.reset()
    assert ov._entry_bar == {} and ov._bar_count == 0


# ---------- 端到端：退出策略确实改变风险特征 ----------

def test_tiered_improves_drawdown_over_plain_hold():
    # 20 天 +6% 后 5 天 -5.5%
    prices = [100.0]
    for i in range(1, 45):
        step = 1.06 if i < 20 else (0.945 if i < 25 else 1.0)
        prices.append(prices[-1] * step)
    data = make_data(prices)

    hold = Engine(ExitOverlay(BuyOnce(), RecordingExit()),
                  config=EngineConfig(initial_cash=1_000_000)).run(data)
    tiered = Engine(ExitOverlay(BuyOnce(), TieredExitStrategy()),
                    config=EngineConfig(initial_cash=1_000_000)).run(data)

    assert tiered.metrics["max_drawdown"] > hold.metrics["max_drawdown"]   # 回撤更小（负值更大）
    assert tiered.metrics["total_return"] >= hold.metrics["total_return"]


def test_registry_strategy_runs_end_to_end():
    for name in ("simple", "tiered", "pressure"):
        ov = ExitOverlay(BuyOnce(), get_exit_strategy(name))
        res = Engine(ov, config=EngineConfig(initial_cash=1_000_000)).run(make_data([100.0] * 8))
        assert res.nav and res.nav[-1][1] > 0


def test_exit_overlay_type_is_strategy():
    assert isinstance(ExitOverlay(BuyOnce(), SimpleExitStrategy()), Strategy)


# ---------- T+N 口径：必须按交易日，不能按自然日 ----------

def test_available_qty_uses_trading_days_not_calendar_days():
    """回归：周四下单、T+3 锁定（成交在周五开盘）。

    成交日 = 周五。交易日口径下 index 差需满 3：周五(0) → 周一(1) → 周二(2) → 周三(3)。
    自然日口径下 周五 + 3 天 = 周一，**周一就会误判为可卖** —— 这正是本用例的判别点。
    """
    days = [date(2026, 3, 5),    # 周四（下单日）
            date(2026, 3, 6),    # 周五（成交日）
            date(2026, 3, 9),    # 周一 ← 自然日口径会误判为可卖
            date(2026, 3, 10),   # 周二
            date(2026, 3, 11)]   # 周三（满 3 个交易日）
    data = {d: {SYM: Bar(symbol=SYM, trade_date=d, open=100.0, high=100.0,
                         low=100.0, close=100.0, pre_close=100.0,
                         volume=1e9, amount=1e9)} for d in days}

    rec = RecordingExit()
    ov = ExitOverlay(BuyOnce(), rec)
    Engine(ov, meta={SYM: {"sellable_after_days": 3}},
           config=EngineConfig(initial_cash=1_000_000)).run(data)

    by_date = {c.trade_date: (c.positions[0].available_qty if c.positions else None)
               for c in rec.calls}
    assert by_date[days[1]] == 0        # 成交当日不可卖
    assert by_date[days[2]] == 0        # 周一：交易日口径不可卖（自然日口径会放行）
    assert by_date[days[3]] == 0
    assert by_date[days[4]] > 0         # 周三满 3 个交易日


