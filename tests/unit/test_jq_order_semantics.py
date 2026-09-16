"""JQRunner order 系列拒单/成交返回值金标准锁定（聚宽口径）。

聚宽语义：拒单一律返回 None 且 res.rejected 追加 (date, symbol, reason)；
成交返回 Order 对象（部分成交也是 Order）。本文件逐路径锁定：
涨跌停 / 停牌 / 资金不足一手 / 可卖截断部分成交 / 正常成交。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest.jqapi import JQRunner

ZERO_COST = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(trade, time="open")
'''


def _flat_df(days=3, start=date(2026, 1, 5), symbol="600000.SH", px=100.0):
    """单标的平价行情：open=close=px，量充足，非涨跌停。"""
    rows = []
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        rows.append(dict(trade_date=d, symbol=symbol, open=px, high=px * 1.005,
                         low=px * 0.995, close=px, pre_close=px if i == 0 else px,
                         volume=1e8, amount=px * 1e8))
    return pl.DataFrame(rows)


def test_limit_up_buy_rejected_returns_none_and_logged():
    """当日开盘一字涨停（open=pre_close*1.10）→ order 返回 None。

    res.rejected 含「涨停不可买」；零成交、零持仓。
    手算：limit=10%，open 121 >= 110*1.1=121 → broker.match 直接拒。
    """
    rows = []
    d = date(2026, 1, 5)
    # 一字涨停：昨收 110，今开=121=110*1.10（含滑点后 121.06 仍 >= 涨停价，二次校验也拦）
    rows.append(dict(trade_date=d, symbol="600000.SH", open=121.0, high=121.0,
                     low=121.0, close=121.0, pre_close=110.0,
                     volume=1e6, amount=1.21e8))
    df = pl.DataFrame(rows)
    code = ZERO_COST + '''
captures = {}

def trade(context):
    captures["ret"] = order_value("600000.SH", 100000)
'''
    runner = JQRunner(code, initial_cash=1_000_000)
    res = runner.run(df)
    assert res.error is None, res.error
    assert runner.ns["captures"]["ret"] is None          # 拒单 → None
    assert res.trades == []
    assert res.rejected == [("2026-01-05", "600000.SH", "涨停不可买")]
    assert not any(res.positions.values())               # 无持仓变化


def test_halted_and_paused_order_returns_none():
    """停牌（volume=0 → halted）下单 → None + rejected「停牌」；

    整日缺 bar（无行情）→ None + rejected「无参考价」。
    """
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    rows = [
        # 停牌日：有 bar 行但 volume=0（Engine.prepare 判 halted）
        dict(trade_date=d1, symbol="600000.SH", open=100.0, high=100.0, low=100.0,
             close=100.0, pre_close=100.0, volume=0.0, amount=0.0),
        # d2 无 600000.SH 的 bar 行；补另一标的保证 d2 仍是交易日
        dict(trade_date=d1, symbol="000001.SZ", open=50.0, high=50.25, low=49.75,
             close=50.0, pre_close=50.0, volume=1e8, amount=5e9),
        dict(trade_date=d2, symbol="000001.SZ", open=50.0, high=50.25, low=49.75,
             close=50.0, pre_close=50.0, volume=1e8, amount=5e9),
    ]
    df = pl.DataFrame(rows)
    code = ZERO_COST + '''
captures = {}

def trade(context):
    captures[str(context.current_dt.date())] = order("600000.SH", 1000)'''
    runner = JQRunner(code, initial_cash=1_000_000)
    res = runner.run(df)
    assert res.error is None, res.error
    cap = runner.ns["captures"]
    assert cap["2026-01-05"] is None                     # 停牌 → None
    assert cap["2026-01-06"] is None                     # 无行情 → None
    assert ("2026-01-05", "600000.SH", "停牌") in res.rejected
    assert ("2026-01-06", "600000.SH", "无参考价") in res.rejected
    assert res.trades == []


def test_insufficient_cash_returns_none_and_sell_truncation_partial_fills():
    """两条资金/可卖路径：

    1) 现金不足一手 → None + rejected「资金不足一手」：
       day0 order_value(1e6) 成交 9900 股（afford=1e6/(100*1.001)=9990.0
       → 整百 9900），余现金 1e6-9900*100.05*1.00001≈9495.09；
       day1 再 order_value(5e5)：afford=9495.09/100.1≈94.86 < 100 股 → 拒单。
    2) 卖出可卖截断 → 部分成交返回 Order（聚宽口径非拒单）：
       day1 卖 order(-10000) 但仅持有 9900 股 → 按 9900 股成交。
    """
    code = ZERO_COST + '''
captures = {}

def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        captures["buy"] = order_value("600000.SH", 1000000)
    else:
        captures["over"] = order_value("600000.SH", 500000)
        captures["sell"] = order("600000.SH", -10000)
'''
    runner = JQRunner(code, initial_cash=1_000_000)
    res = runner.run(_flat_df(days=2))
    assert res.error is None, res.error
    cap = runner.ns["captures"]
    # day0：正常买入，返回 Order
    assert cap["buy"] is not None
    assert cap["buy"].filled_qty == 9900.0
    # day1：满额再买 → 资金不足一手 → None + rejected
    assert cap["over"] is None
    assert any(r[2] == "资金不足一手" for r in res.rejected)
    # day1：超额卖出 → 截断到可卖 9900 股，部分成交仍返回 Order
    assert cap["sell"] is not None
    assert cap["sell"].filled_qty == 9900.0
    sells = [t for t in res.trades if t.side.value == "sell"]
    assert len(sells) == 1 and sells[0].qty == 9900.0


def test_successful_order_returns_order_with_filled_avg_price():
    """正常成交返回 Order；平均成交价 = 开盘价 * (1 + 0.0005)（PctSlippage 默认万五）。

    手算：open=100 → 买入成交价 100.05；Order.filled_amount/filled_qty = 100.05。
    """
    code = ZERO_COST + '''
captures = {}

def trade(context):
    captures["ret"] = order("600000.SH", 1000)
'''
    runner = JQRunner(code, initial_cash=1_000_000)
    res = runner.run(_flat_df(days=1))
    assert res.error is None, res.error
    o = runner.ns["captures"]["ret"]
    assert o is not None
    assert o.filled_qty == 1000.0
    assert o.filled_amount / o.filled_qty == pytest.approx(100 * 1.0005)
    assert len(res.trades) == 1
    assert res.trades[0].price == pytest.approx(100.05)
