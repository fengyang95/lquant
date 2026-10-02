"""退出策略：信号模型、三个内置实现、注册表。"""
from __future__ import annotations

from datetime import date

import pytest

from lquant.backtest.events import Bar
from lquant.backtest.exit import (
    EXIT_STRATEGIES,
    ExitContext,
    ExitSignal,
    ExitType,
    PositionView,
    PressureExitStrategy,
    SimpleExitStrategy,
    TieredExitStrategy,
    get_exit_strategy,
    is_sealed_limit_down,
)

D = date(2026, 3, 2)


def bar(close: float, *, pre_close: float | None = None, high: float | None = None,
        low: float | None = None, open_: float | None = None,
        fields: dict | None = None, suspended: bool = False,
        halted: bool = False) -> Bar:
    return Bar(symbol="600519.SH", trade_date=D, open=open_ if open_ is not None else close,
               high=high if high is not None else close,
               low=low if low is not None else close,
               close=close, pre_close=pre_close if pre_close is not None else close,
               volume=1e6, amount=1e8, suspended=suspended, halted=halted,
               fields=fields or {})


def ctx(b: Bar, pos: PositionView | None = None) -> ExitContext:
    return ExitContext(trade_date=D, phase="close",
                       positions=[pos] if pos else [], bars={b.symbol: b})


def pos(avg_cost: float = 100.0, qty: float = 1000.0, available: float | None = None,
        holding_days: int = 0) -> PositionView:
    return PositionView(symbol="600519.SH", qty=qty,
                        available_qty=qty if available is None else available,
                        avg_cost=avg_cost, holding_days=holding_days)


# ---------- 注册表与信号模型 ----------

def test_registry_lists_builtins():
    assert EXIT_STRATEGIES.keys() == ["pressure", "simple", "tiered"]
    assert all({"name", "label"} <= set(d) for d in EXIT_STRATEGIES.describe())


def test_get_exit_strategy_instantiates_with_params():
    s = get_exit_strategy("simple", stop_loss_pct=8.0)
    assert isinstance(s, SimpleExitStrategy) and s.stop_loss_pct == 8.0


def test_get_exit_strategy_unknown_raises():
    with pytest.raises(KeyError, match="未注册"):
        get_exit_strategy("nope")


def test_exit_signal_ratio_bounds():
    ExitSignal("600519.SH", 1.0, ExitType.STOP_LOSS)
    ExitSignal("600519.SH", 1 / 3, ExitType.CHANNEL)
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="ratio"):
            ExitSignal("600519.SH", bad, ExitType.STOP_LOSS)


def test_position_view_helpers():
    p = pos(avg_cost=100.0, qty=1000.0, available=300.0)
    assert p.sellable == 300.0
    assert p.pnl_pct(110.0) == pytest.approx(10.0)
    assert p.pnl_pct(90.0) == pytest.approx(-10.0)
    assert PositionView("x", 1.0, 1.0, 0.0).pnl_pct(10.0) == 0.0


def test_sealed_limit_down_detection():
    assert is_sealed_limit_down(bar(9.0, pre_close=10.0, high=9.0, low=9.0)) is True
    # 一字涨停能卖出，不算封死
    assert is_sealed_limit_down(bar(11.0, pre_close=10.0, high=11.0, low=11.0)) is False
    # 有振幅就不算一字板
    assert is_sealed_limit_down(bar(9.5, pre_close=10.0, high=9.9, low=9.1)) is False
    assert is_sealed_limit_down(bar(9.0, pre_close=0.0, high=9.0, low=9.0)) is False


# ---------- SimpleExitStrategy ----------

def test_simple_stop_loss_close_mode():
    s = SimpleExitStrategy(stop_loss_pct=10.0)
    assert s.on_bar(ctx(bar(91.0), pos())) == []          # 91 > 90 未破位
    touch = s.on_bar(ctx(bar(90.0), pos()))               # 触及止损线即触发
    assert touch and touch[0].exit_type == ExitType.STOP_LOSS
    hits = s.on_bar(ctx(bar(89.0), pos()))                # 跌破 90
    assert len(hits) == 1 and hits[0].exit_type == ExitType.STOP_LOSS
    assert hits[0].ratio == 1.0


def test_simple_stop_intraday_mode_uses_open_when_gapped():
    s = SimpleExitStrategy(stop_loss_pct=10.0, stop_mode="intraday")
    hit = s.on_bar(ctx(bar(95.0, low=85.0, open_=85.0), pos()))[0]
    assert hit.ref_price == 85.0                          # 跳空低开按开盘价
    hit2 = s.on_bar(ctx(bar(95.0, low=85.0, open_=99.0), pos()))[0]
    assert hit2.ref_price == 90.0                         # 盘中破位按止损价


def test_simple_close_mode_ignores_intraday_wick():
    s = SimpleExitStrategy(stop_loss_pct=10.0, stop_mode="close")
    assert s.on_bar(ctx(bar(95.0, low=80.0), pos())) == []


def test_simple_sealed_limit_down_defers_exit():
    """一字跌停卖不出去，不能生成注定无法成交的卖单。"""
    s = SimpleExitStrategy(stop_loss_pct=10.0)
    sealed = bar(88.0, pre_close=97.8, high=88.0, low=88.0)
    assert s.on_bar(ctx(sealed, pos())) == []


def test_simple_take_profit():
    s = SimpleExitStrategy(take_profit_pct=20.0)
    assert s.on_bar(ctx(bar(119.0), pos())) == []
    hit = s.on_bar(ctx(bar(121.0), pos()))[0]
    assert hit.exit_type == ExitType.TAKE_PROFIT


def test_simple_time_stop_respects_min_return():
    s = SimpleExitStrategy(time_stop_days=20, time_stop_min_return=5.0)
    losing = s.on_bar(ctx(bar(101.0), pos(holding_days=21)))
    assert losing and losing[0].exit_type == ExitType.TIME_STOP
    winning = s.on_bar(ctx(bar(110.0), pos(holding_days=21)))   # +10% > 5%
    assert winning == []
    young = s.on_bar(ctx(bar(101.0), pos(holding_days=5)))
    assert young == []


def test_simple_skips_unsellable_and_missing_bars():
    s = SimpleExitStrategy(stop_loss_pct=10.0)
    assert s.on_bar(ctx(bar(80.0), pos(available=0.0))) == []       # T+N 未解禁
    assert s.on_bar(ctx(bar(80.0, suspended=True), pos())) == []
    assert s.on_bar(ctx(bar(80.0, halted=True), pos())) == []


def test_simple_validates_params():
    with pytest.raises(ValueError, match="stop_loss_pct"):
        SimpleExitStrategy(stop_loss_pct=0)
    with pytest.raises(ValueError, match="stop_mode"):
        SimpleExitStrategy(stop_mode="weekly")


# ---------- TieredExitStrategy ----------

def test_tiered_tolerance_table():
    s = TieredExitStrategy()
    assert s.drawdown_tolerance(120.0) == 5.0
    assert s.drawdown_tolerance(50.0) == 5.0
    assert s.drawdown_tolerance(30.0) == 8.0
    assert s.drawdown_tolerance(29.9) == 10.0
    assert s.drawdown_tolerance(-5.0) == 10.0


def test_tiered_trailing_triggers_on_pullback():
    s = TieredExitStrategy()
    # 冲高到 200（+100% → 容忍 5%），止盈线 = 190
    assert s.on_bar(ctx(bar(200.0, high=200.0), pos())) == []
    hits = s.on_bar(ctx(bar(180.0, high=200.0), pos()))
    assert hits and hits[0].exit_type == ExitType.TRAILING
    assert hits[0].ref_price == 180.0


def test_tiered_trailing_is_ratchet_only_up():
    s = TieredExitStrategy()
    p = pos()
    s.on_bar(ctx(bar(200.0, high=200.0), p))
    up = s.trailing_stop_level(p, 200.0)
    s._peak["600519.SH"] = 150.0                 # 人为压低峰值
    assert s.trailing_stop_level(p, 150.0) == up  # 只上移不下移


def test_tiered_stop_loss_has_priority_over_trailing():
    s = TieredExitStrategy(stop_loss_pct=10.0)
    s.on_bar(ctx(bar(150.0, high=150.0), pos()))
    hits = s.on_bar(ctx(bar(85.0, high=150.0), pos()))
    assert hits[0].exit_type == ExitType.STOP_LOSS


def test_tiered_default_time_stop_is_20d_below_5pct():
    s = TieredExitStrategy()
    assert s.time_stop_days == 20 and s.time_stop_min_return == 5.0
    hits = s.on_bar(ctx(bar(101.0), pos(holding_days=20)))
    assert hits and hits[0].exit_type == ExitType.TIME_STOP


def test_tiered_reset_clears_state():
    s = TieredExitStrategy()
    s.on_bar(ctx(bar(200.0, high=200.0), pos()))
    s.reset()
    assert s._peak == {} and s._trailing_stop == {}


def test_tiered_validates_tiers():
    with pytest.raises(ValueError, match="容忍回撤"):
        TieredExitStrategy(tiers=((50.0, 0.0),))
    with pytest.raises(ValueError, match="base_drawdown_pct"):
        TieredExitStrategy(base_drawdown_pct=0)


def test_tiered_peak_dropped_when_position_closed():
    s = TieredExitStrategy()
    s.on_bar(ctx(bar(200.0, high=200.0), pos()))
    assert "600519.SH" in s._peak
    s.on_bar(ctx(bar(200.0, high=200.0), None))       # 已无持仓
    assert "600519.SH" not in s._peak


# ---------- PressureExitStrategy ----------

def test_pressure_a_tier_then_b_tier():
    s = PressureExitStrategy()
    p = pos()
    b1 = bar(105.0, high=110.0, fields={"td_jinniu": 108.0})
    assert s.on_bar(ctx(b1, p)) == []                 # 首根无 REF，不触发
    b2 = bar(105.0, high=110.0, fields={"td_jinniu": 108.0})
    hits = s.on_bar(ctx(b2, p))                       # 上一根压力位 108，high 110 触及
    assert len(hits) == 1 and hits[0].exit_type == ExitType.CHANNEL
    assert hits[0].ratio == pytest.approx(1 / 3)
    b3 = bar(100.0, fields={"td_jinniu": 108.0})
    hits2 = s.on_bar(ctx(b3, p))                      # close 100 < 压力 108 → B 档
    assert hits2 and hits2[0].ratio == pytest.approx(0.5)


def test_pressure_falls_back_to_tiered_without_field():
    s = PressureExitStrategy()
    p = pos()
    s.on_bar(ctx(bar(200.0, high=200.0), p))
    hits = s.on_bar(ctx(bar(180.0, high=200.0), p))
    assert hits and hits[0].exit_type == ExitType.TRAILING   # 无压力列 → 退化为分级止盈


def test_pressure_hard_stop_wins_over_channel():
    s = PressureExitStrategy(stop_loss_pct=10.0)
    p = pos()
    s.on_bar(ctx(bar(105.0, high=110.0, fields={"td_jinniu": 108.0}), p))
    hits = s.on_bar(ctx(bar(85.0, high=110.0, fields={"td_jinniu": 108.0}), p))
    assert hits[0].exit_type == ExitType.STOP_LOSS


def test_pressure_validates_ratios_and_resets_stage():
    with pytest.raises(ValueError, match="ratio_a"):
        PressureExitStrategy(ratio_a=0.0)
    with pytest.raises(ValueError, match="ratio_b"):
        PressureExitStrategy(ratio_b=2.0)
    s = PressureExitStrategy()
    p = pos()
    s.on_bar(ctx(bar(105.0, high=110.0, fields={"td_jinniu": 108.0}), p))
    s.on_bar(ctx(bar(105.0, high=110.0, fields={"td_jinniu": 108.0}), p))
    assert s._stage["600519.SH"] == 1
    s.reset()
    assert s._stage == {}
