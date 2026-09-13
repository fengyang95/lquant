"""复杂策略对拍测试：JQRunner 与独立参照实现的交叉验证。

场景均先在 data/jq_complex_check.py 里与逐笔对拍参照实现验证通过，
这里固化其精确期望值（成交三元组 / NAV / 拒单原因）。
覆盖：多标的轮动、order_target_value 调仓、T+1、止损、涨跌停、停牌、
run_weekly/run_monthly 调度、FixedSlippage、最低佣金与过户费。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as plt

from lquant.backtest.jqapi import JQRunner

SYMS = ["600000.SH", "600519.SH", "000001.SZ"]


def bdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_bars(rows: list[dict]) -> plt.DataFrame:
    need = ["trade_date", "symbol", "open", "high", "low", "close",
            "pre_close", "volume", "amount"]
    rows = [{**r, "volume": r.get("volume", 1e6), "amount": r.get("amount", 1e7)}
            for r in rows]
    return plt.DataFrame(rows).select(need).sort(["trade_date", "symbol"])


def _s1_data() -> plt.DataFrame:
    dates = bdays(date(2026, 1, 5), 30)          # 1/5 是周一
    paths = {SYMS[0]: [10 * (1.01 ** i) for i in range(30)],
             SYMS[1]: [50 * (1 - 0.005 * (i % 5) / 2) for i in range(30)],
             SYMS[2]: [20 * (1.03 ** (i // 3)) * (1.0 if i % 3 else 0.99) for i in range(30)]}
    rows = []
    for i, d in enumerate(dates):
        for s in SYMS:
            px = round(paths[s][i], 2)
            pre = round(paths[s][i - 1], 2) if i else px
            rows.append(dict(trade_date=d, symbol=s, open=px, high=px * 1.01,
                             low=px * 0.99, close=round(px * 1.002, 2), pre_close=pre))
    return make_bars(rows)


CODE_ROTATION = '''
def initialize(context):
    set_order_cost(type='stock', open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    run_weekly(rebalance, weekday=1, time='open')

def rebalance(context):
    scores = {}
    for s in ['600000.SH', '600519.SH', '000001.SZ']:
        h = attribute_history(s, 6, '1d', ['close'])
        if len(h) < 6:
            return
        scores[s] = h['close'][-1] / h['close'][0] - 1
    top = max(scores, key=scores.get)
    for s, p in list(context.portfolio.positions.items()):
        if s != top and p.total_amount > 0:
            order_target(s, 0)
    order_target_value(top, context.portfolio.total_value)
'''


def test_rotation_matches_independent_reference():
    """动量轮动：与独立参照实现（含 0.1% 资金缓冲、整手、过户费）逐笔一致。"""
    df = _s1_data()
    closes = {(r["trade_date"], r["symbol"]): r["close"] for r in df.to_dicts()}
    opens = {(r["trade_date"], r["symbol"]): r["open"] for r in df.to_dicts()}
    dates = sorted({r["trade_date"] for r in df.to_dicts()})

    res = JQRunner(CODE_ROTATION, initial_cash=1_000_000.0).run(df)
    assert res.error is None, res.error

    # 独立参照实现（镜像引擎语义：open 撮合、整手、afford 缓冲、过户费万 0.1）
    cash, pos, ref_trades, ref_nav = 1_000_000.0, {}, [], []
    for i, d in enumerate(dates):
        if d.weekday() == 0 and i >= 6:
            scores = {s: closes[(dates[i - 1], s)] / closes[(dates[i - 6], s)] - 1
                      for s in SYMS}
            top = max(scores, key=scores.get)
            tv = cash + sum(q * opens[(d, s)] for s, q in pos.items())
            for s, q in list(pos.items()):
                if s != top and q > 0:
                    cash += q * opens[(d, s)] * (1 - 1e-5)
                    ref_trades.append((d, s, "sell", q, opens[(d, s)]))
                    del pos[s]
            px = opens[(d, top)]
            held_val = pos.get(top, 0) * px
            amt = (tv - held_val) / px
            qty = min(amt, cash / (px * 1.001))
            qty = int(qty) // 100 * 100
            if qty > 0:
                cash -= qty * px * (1 + 1e-5)
                pos[top] = pos.get(top, 0) + qty
                ref_trades.append((d, top, "buy", qty, px))
        ref_nav.append(round(cash + sum(q * closes[(d, s)] for s, q in pos.items()), 2))

    eng = [(t.trade_date, t.symbol, t.side.value.lower(), t.qty, round(t.price, 6))
           for t in res.trades]
    ref = [(d, s, side, q, round(p, 6)) for d, s, side, q, p in ref_trades]
    assert eng == ref
    nav_diff = max(abs(a - b) for (_, a), b in zip(res.nav, ref_nav, strict=True))
    assert nav_diff <= 0.02


def test_rotation_negative_index_idiom():
    """聚宽社区主流写法 h['close'][-1] / h['close'][0] 必须可用。"""
    res = JQRunner(CODE_ROTATION, initial_cash=1_000_000.0).run(_s1_data())
    assert res.error is None, res.error
    assert res.trades          # 若负数下标失效会静默空仓


def test_stop_loss_t_plus_1():
    """止损 + T+1：当日买的不能当日卖；次日/之后可卖；止损后再入场。"""
    d2 = bdays(date(2026, 2, 2), 4)
    px = [10.0, 9.5, 9.0, 9.2]                   # day3 收 8.955 触发止损
    rows = [dict(trade_date=d2[i], symbol="600000.SH", open=px[i], high=px[i] * 1.02,
                 low=px[i] * 0.98, close=round(px[i] * 0.995, 2),
                 pre_close=px[i - 1] if i else 10.0) for i in range(4)]
    code = '''
def initialize(context):
    set_order_cost(type='stock', open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    g.sold = False

def handle_data(context, data):
    sec = '600000.SH'
    pos = context.portfolio.positions[sec].total_amount
    if pos == 0:
        order_value(sec, 100_000)
    elif not g.sold and data[sec].close < 9.2:
        order_target(sec, 0)
        g.sold = True
'''
    res = JQRunner(code, initial_cash=1_000_000.0).run(make_bars(rows))
    assert res.error is None, res.error
    got = [(t.trade_date, t.side.value.lower(), t.qty, round(t.price, 4))
           for t in res.trades]
    assert got[0] == (d2[0], "buy", 10000.0, 10.0)
    assert (d2[2], "sell", 10000.0, 9.0) in got


def test_same_day_sell_blocked():
    """同日先买后卖被 T+1 拦截：仅 1 笔成交。"""
    d2 = bdays(date(2026, 2, 2), 2)
    rows = [dict(trade_date=d2[i], symbol="600000.SH", open=10.0, high=10.2, low=9.8,
                 close=10.0, pre_close=10.0) for i in range(2)]
    code = '''
def initialize(context):
    set_order_cost(type='stock', open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))

def handle_data(context, data):
    if context.portfolio.positions['600000.SH'].total_amount == 0:
        order_value('600000.SH', 100_000)
        order_target('600000.SH', 0)
'''
    res = JQRunner(code, initial_cash=1_000_000.0).run(make_bars(rows))
    assert res.error is None
    assert len(res.trades) == 1 and res.trades[0].side.value.lower() == "buy"


def test_limit_and_halt_rejections():
    """涨停拒买、跌停拒卖、停牌拒单，正常日不受影响。"""
    d3 = bdays(date(2026, 3, 2), 5)
    prices = [10.0, 11.0, 10.5, 9.45, 10.0]      # day2 涨停开盘; day4 跌停开盘
    rows = []
    for i, d in enumerate(d3):
        r = dict(trade_date=d, symbol="600000.SH", open=prices[i], high=prices[i] * 1.02,
                 low=prices[i] * 0.98, close=round(prices[i] * 0.99, 2),
                 pre_close=prices[i - 1] if i else 10.0)
        if i == 4:
            r["volume"] = 0.0                     # 停牌
            r["close"] = prices[3]
        rows.append(r)
    code = '''
def initialize(context):
    set_order_cost(type='stock', open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    g.stage = 0

def handle_data(context, data):
    sec = '600000.SH'
    pos = context.portfolio.positions[sec].total_amount
    if pos == 0 and g.stage == 0:
        order_value(sec, 100_000)
        g.stage = 1
    elif g.stage == 1:
        order_value(sec, 50_000)
        g.stage = 2
    elif g.stage == 2:
        order_value(sec, 50_000)
        g.stage = 3
    elif g.stage == 3:
        order_target(sec, 0)
        g.stage = 4
    elif g.stage == 4:
        order_target(sec, 0)
'''
    res = JQRunner(code, initial_cash=1_000_000.0).run(make_bars(rows))
    assert res.error is None
    rej = {r[2] for r in res.rejected}
    assert any("涨停" in x for x in rej)
    assert any("跌停" in x for x in rej)
    assert any("停牌" in x for x in rej)
    sides = [(t.trade_date, t.side.value.lower()) for t in res.trades]
    assert (d3[0], "buy") in sides and (d3[2], "buy") in sides
    assert not any(t[0] == d3[1] for t in sides)


def test_weekly_monthly_schedule():
    """run_weekly(weekday=1) 命中所有 ISO 周一；run_monthly(monthday=1) 命中每月首个交易日。"""
    d4 = bdays(date(2026, 1, 5), 45)
    rows = [dict(trade_date=d, symbol="600000.SH", open=10.0 + i * 0.1,
                 high=10.0 + i * 0.1, low=10.0 + i * 0.1, close=10.0 + i * 0.1,
                 pre_close=10.0 if i == 0 else 10.0 + (i - 1) * 0.1)
            for i, d in enumerate(d4)]
    code = '''
def initialize(context):
    g.w, g.m = [], []
    run_weekly(lambda c: g.w.append(c.current_dt.date().isoformat()), weekday=1)
    run_monthly(lambda c: g.m.append(c.current_dt.date().isoformat()), monthday=1)

def handle_data(context, data):
    pass
'''
    runner = JQRunner(code, initial_cash=1_000_000.0)
    res = runner.run(make_bars(rows))
    assert res.error is None
    nth = [d for i, d in enumerate(d4)
           if not any(x.year == d.year and x.month == d.month for x in d4[:i])]
    assert runner.ns["g"].w == [d.isoformat() for d in d4 if d.weekday() == 0]
    assert runner.ns["g"].m == [d.isoformat() for d in nth]


def test_slippage_and_min_commission():
    """FixedSlippage(0.02) → 买价 +0.01；最低佣金 5 元兜底；过户费万 0.1 另计。"""
    d5 = bdays(date(2026, 4, 1), 3)
    rows = [dict(trade_date=d5[i], symbol="600000.SH", open=10.0, high=10.2, low=9.8,
                 close=10.0, pre_close=10.0) for i in range(3)]
    code = '''
def initialize(context):
    set_order_cost(type='stock', open_tax=0, close_tax=0,
                   open_commission=0.0001, close_commission=0.0001, min_commission=5)
    set_slippage(FixedSlippage(0.02))

def handle_data(context, data):
    if context.portfolio.positions['600000.SH'].total_amount == 0:
        order_value('600000.SH', 10_000)
'''
    res = JQRunner(code, initial_cash=1_000_000.0).run(make_bars(rows))
    assert res.error is None
    t = res.trades[0]
    assert round(t.price, 4) == 10.01
    assert t.qty == 1000.0                       # order_value 按参考价（无滑点）折股
    fee_expect = max(5, 1000 * 10.01 * 0.0001) + 1000 * 10.01 * 0.00001
    assert abs(t.fee - fee_expect) < 1e-9
