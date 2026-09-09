"""JQRunner：before_trading_start / after_trading_end / record() 采集。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest.jqapi import JQRunner


def make_df(days: int = 2, start: date = date(2026, 1, 5)) -> pl.DataFrame:
    """两日两标的合成行情：600519.SH 基准 100（日 +1），000001.SZ 基准 50（日 +1）。"""
    rows = []
    base = {"600519.SH": 100.0, "000001.SZ": 50.0}
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        for s, b in base.items():
            px = b + i
            pre = b + i - 1 if i else b
            rows.append(dict(trade_date=d, symbol=s, open=px, high=px * 1.005,
                             low=px * 0.995, close=px, pre_close=pre,
                             volume=1e8, amount=px * 1e8))
    return pl.DataFrame(rows)


DF = make_df()
DATES = [date(2026, 1, 5), date(2026, 1, 6)]

CODE = """
calls = []

def initialize(context):
    run_daily(rebalance, time='open')

def before_trading_start(context):
    record(cash=context.portfolio.available_cash)
    calls.append(('pre', context.current_dt.date()))

def rebalance(context):
    if not context.portfolio.positions["600519.SH"].total_amount:
        order('600519.SH', 100)
    calls.append(('sched', context.current_dt.date()))

def after_trading_end(context):
    calls.append(('post', context.current_dt.date()))
"""


def test_lifecycle_and_record():
    runner = JQRunner(CODE, initial_cash=1_000_000)
    res = runner.run(make_df())
    assert res.error is None

    # record：每日一条，首日记录的是下单前的初始资金
    assert "cash" in res.records
    rec = res.records["cash"]
    assert [d for d, _ in rec] == DATES
    assert rec[0][1] == pytest.approx(1_000_000, rel=1e-9)
    assert rec[1][1] < 1_000_000                      # 第二天已持仓，现金减少
    assert res.metrics["n_trades"] == 1

    # 顺序：每日 pre → 调度(open) → post
    expect = [(tag, d) for d in DATES for tag in ("pre", "sched", "post")]
    assert runner.ns["calls"] == expect


def test_no_hooks_still_runs():
    """未定义钩子的旧策略不受影响。"""
    code = """
def initialize(context):
    run_daily(buy, time='open')

def buy(context):
    order('000001.SZ', 100)
"""
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is None
    assert res.metrics["n_trades"] == 2               # 两日各买一次
    assert res.records == {}


def test_hook_exception_captured():
    code = """
def before_trading_start(context):
    raise ValueError('boom')
"""
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is not None
    assert "boom" in res.error
