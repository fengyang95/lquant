"""backtest.engine 缺口分支覆盖：拒单路径、same_close 模式与防御分支。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest import engine as eng
from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.strategy.base import Strategy


class _Fixed(Strategy):
    def __init__(self, targets, only_dates=None, **params):
        super().__init__(**params)
        self.targets = targets
        self.only_dates = only_dates

    def on_bar(self, ctx, bars):
        if self.only_dates and ctx.trade_date not in self.only_dates:
            return []
        return self.targets


def _bar(sym, d, *, close=10.0, adj=1.0, halted=False, suspended=False,
         vol=1e6):
    return Bar(symbol=sym, trade_date=d, open=close, high=close, low=close,
               close=close, pre_close=close, volume=vol, amount=vol * close,
               adj_factor=adj, halted=halted, suspended=suspended)


def _df_two_days():
    rows = []
    for d in (date(2026, 1, 5), date(2026, 1, 6)):
        for s in ("600000", "000001"):
            rows.append({
                "trade_date": d, "symbol": s, "open": 10.0, "high": 10.0,
                "low": 10.0, "close": 10.0, "pre_close": 10.0,
                "volume": 1e6, "amount": 1e7,
            })
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def test_prepare_missing_column_raises() -> None:
    with pytest.raises(KeyError, match="缺少列"):
        Engine.prepare(_df_two_days().drop("high"))


def test_run_empty_input_returns_empty_result() -> None:
    res = Engine(_Fixed([])).run({})
    assert res.nav == [] and res.trades == [] and res.metrics == {}


def test_run_nav_nonpositive_raises(monkeypatch) -> None:
    monkeypatch.setattr(eng.Account, "nav", lambda self, prices, last: 0.0)
    with pytest.raises(ValueError, match="NAV"):
        Engine(_Fixed([])).run(_df_two_days())


def test_corporate_action_skip_branches() -> None:
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    data = {d1: {"600000": _bar("600000", d1, adj=1.0)},
            d2: {"600001": _bar("600001", d2, adj=0.0)}}
    e = Engine(_Fixed([]), config=EngineConfig(rebalance="none"))
    e.run(data)
    assert e.account.positions == {}


def test_fill_pending_no_bar_records_rejected() -> None:
    d2 = date(2026, 1, 6)
    e = Engine(_Fixed([]), config=EngineConfig(price_mode="next_open"))
    e.broker = object()
    e._rules = {}
    e.account = eng.Account(cash=1_000_000)
    e._pending = [Order(order_id="oX", symbol="999999.SH", side=Side.BUY,
                        qty=100.0)]
    res = eng.BacktestResult()
    e._fill_pending({"600000": _bar("600000", d2)}, d2, res)
    assert res.rejected[0][2] == "无行情"
    assert e._pending == []


def test_schedule_rebalance_nav_nonpositive_returns(monkeypatch) -> None:
    monkeypatch.setattr(eng.Account, "nav", lambda self, prices, last: 0.0)
    d = date(2026, 1, 5)
    e = Engine(_Fixed([("600000", 1.0)]), config=EngineConfig())
    e.broker = object()
    e._rules = eng.build_rules(["600000.SH"])
    e.account = eng.Account(cash=1_000_000)
    res = eng.BacktestResult()
    e._schedule_rebalance({"600000": _bar("600000", d)}, d, res)
    assert res.nav == [] and res.trades == []


def test_schedule_rebalance_missing_bar_price_skips_symbol() -> None:
    """目标 symbol 当日无 bar → 跳过，不生成订单。"""
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    e = Engine(_Fixed([("600000", 0.5)]),
               config=EngineConfig(price_mode="next_open"))
    data = {
        d1: {"600000": _bar("600000", d1), "000001": _bar("000001", d1)},
        d2: {"000001": _bar("000001", d2)},
    }
    res = e.run(data)
    # d1 挂的买单在 d2 因无行情被拒
    assert res.rejected[0][2] == "无行情"
    assert e._pending == []


def test_sell_qty_no_position_returns_zero() -> None:
    e = Engine(_Fixed([]))
    e._rules = eng.build_rules(["600000.SH"])
    assert e._sell_qty("600000.SH", 10.0, date(2026, 1, 5)) == 0.0


def _same_close(*, halted=False, suspended=False, missing=False,
                participation=0.1):
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    bars2 = {} if missing else {
        "600000": _bar("600000", d2, halted=halted, suspended=suspended)}
    # 仅 d2 发信号 → 订单在 same_close 下当日撮合，落到待测分支
    strat = _Fixed([("600000", 0.5)], only_dates={d2})
    e = Engine(strat,
               config=EngineConfig(price_mode="same_close",
                                   participation=participation))
    return e.run({d1: {"600000": _bar("600000", d1)}, d2: bars2})


def test_same_close_missing_bar_generates_no_order() -> None:
    # 目标 symbol 当日无 bar → 无价格可估 → 不生成订单（拒单分支不可达，防御性代码）
    res = _same_close(missing=True)
    assert res.rejected == [] and res.trades == []


def test_same_close_suspended_rejected() -> None:
    assert _same_close(suspended=True).rejected[0][2] == "suspended"


def test_same_close_halted_rejected() -> None:
    assert _same_close(halted=True).rejected[0][2] == "停牌或无行情"


def test_same_close_unfilled_rejected() -> None:
    # participation=0 → broker.match 量截断为 0 → 返回 None → 未成交
    assert _same_close(participation=0.0).rejected[0][2] == "数量不足一手或资金不足"
