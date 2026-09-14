"""聚宽兼容回测 API 测试：金标准手算 + 防未来 + T+1 + 费率覆盖 + 涨跌停。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest.jqapi import JQRunner


def make_df(days=6, start=date(2026, 1, 5)):
    """两只股票：A 每天 +2%，B 每天 -1%。开盘=收盘（简化撮合）。"""
    rows = []
    px = {"600000.SH": 100.0, "000001.SZ": 50.0}
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        for s, base in px.items():
            p = base * (1.02 ** i) if s.startswith("6") else base * (0.99 ** i)
            pre = p if i == 0 else (
                base * (1.02 ** (i - 1)) if s.startswith("6") else base * (0.99 ** (i - 1)))
            rows.append(dict(trade_date=d, symbol=s, open=p, high=p * 1.005, low=p * 0.995,
                             close=p, pre_close=pre, volume=1e8, amount=p * 1e8))
    return pl.DataFrame(rows)


def test_buy_and_hold_golden():
    """零费率开盘买入并持有：净值 = 现金 + 持股×收盘价，逐日手算对照。

    买入价 100.05（万五滑点）→ 现金 499,744.9975（含 5.0025 过户费，
    过户费不在 set_order_cost 覆盖范围，按规则表收取）。
    """
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(buy, time="open")

def buy(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_target_value("600000.SH", 500000)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is None
    assert len(res.trades) == 1
    assert res.trades[0].qty == 5000.0                       # 500000/100，整百
    cash = 1_000_000 - 5000 * 100.05 * (1 + 1e-5)            # 滑点价 + 过户费
    for k, (_d, v) in enumerate(res.nav):
        px = 100.0 * (1.02 ** k)
        expect = cash + 5000 * px
        assert v == pytest.approx(expect, rel=1e-6), f"day {k}"
    assert res.nav[-1][1] == pytest.approx(cash + 5000 * 100 * 1.02 ** 5, rel=1e-6)


def test_order_target_value_rebalance_to_half():
    """order_target_value 半仓调仓：两只标的各 ~50%。"""
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_monthly(rebal, monthday=1, time="open")

def rebal(context):
    half = context.portfolio.total_value / 2
    order_target_value("600000.SH", half)
    order_target_value("000001.SZ", half)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is None
    assert len(res.trades) == 2                              # 只在第一个交易日调仓一次
    last_pos = res.positions[max(res.positions)]
    assert set(last_pos) == {"600000.SH", "000001.SZ"}


def test_t_plus_1_sell_rejected_same_day():
    """当日开盘买入，当日收盘 closeable=0 不能卖；首笔卖出必然晚于首笔买入。"""
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(trade, time="open")
    run_daily(try_sell, time="close")

def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_value("600000.SH", 200000)

def try_sell(context):
    if context.portfolio.positions["600000.SH"].closeable_amount > 0:
        order_target("600000.SH", 0)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is None
    buys = [t for t in res.trades if t.side.value == "buy"]
    sells = [t for t in res.trades if t.side.value == "sell"]
    assert buys and sells
    assert sells[0].trade_date > buys[0].trade_date          # T+1


def test_history_values_from_namespace():
    """第 4 个交易日 history(3) 应等于前 3 日收盘 [100, 102, 104.04]；首日为空。"""
    code = '''
captured = {}
def initialize(context):
    run_daily(check, time="open")

def check(context):
    h = history(3, field="close", security_list=["600000.SH"])
    captured[str(context.current_dt.date())] = list(h["600000.SH"])
'''
    runner = JQRunner(code, initial_cash=1_000_000)
    res = runner.run(make_df())
    assert res.error is None
    cap = runner.ns["captured"]
    days = sorted(cap)
    assert cap[days[0]] == []                                # 首日无历史（防未来）
    assert cap[days[3]] == pytest.approx([100.0, 102.0, 104.04])


def test_set_order_cost_fees_applied():
    """费率覆盖生效：万三佣金最低 5 元、千一印花税只在卖出收。"""
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0.001,
                   open_commission=0.0003, close_commission=0.0003, min_commission=5)
    run_daily(trade, time="open")
    run_daily(sell_tmr, time="close")

def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_value("600000.SH", 100000)

def sell_tmr(context):
    if context.portfolio.positions["600000.SH"].closeable_amount >= 1000 \
            and len([o for o in _trades()]) == 0:
        order_target("600000.SH", 0)

def _trades():
    return []
'''
    # 上面 sell_tmr 复杂了：换个简单策略——持有到最后一天全部卖出
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0.001,
                   open_commission=0.0003, close_commission=0.0003, min_commission=5)
    run_daily(trade, time="open")
    run_daily(sell_if_any, time="close")

def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_value("600000.SH", 100000)

def sell_if_any(context):
    pass
'''
    runner = JQRunner(code, initial_cash=1_000_000)
    res = runner.run(make_df())
    assert res.error is None
    buy = res.trades[0]
    assert buy.fee == pytest.approx(max(5, buy.qty * buy.price * 0.0003) + buy.qty * buy.price * 1e-5,
                                    rel=1e-6)
    # 手动触发一笔卖出验证印花税
    runner._today = list(runner._dates)[-1]
    runner._day_index = len(runner._dates) - 1
    runner._bars_today = runner._bars_by_day[runner._today]
    runner._bucket = "close"
    o = runner._submit("600000.SH", -1000)                   # 负数 = 卖出 1000 股
    assert o is not None
    sell = res.trades[-1]
    assert sell.side.value == "sell"
    assert sell.fee == pytest.approx(max(5, sell.qty * sell.price * 0.0003)
                                     + sell.qty * sell.price * 0.001
                                     + sell.qty * sell.price * 1e-5, rel=1e-6)


def test_limit_up_buy_rejected():
    """开盘一字涨停 → 拒单（与撮合器共用规则）。"""
    rows = []
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    rows.append(dict(trade_date=d1, symbol="600000.SH", open=100.0, high=110.0, low=100.0,
                     close=110.0, pre_close=100.0, volume=1e8, amount=1e10))
    rows.append(dict(trade_date=d2, symbol="600000.SH", open=121.0, high=121.0, low=121.0,
                     close=121.0, pre_close=110.0, volume=1e6, amount=1.2e8))
    df = pl.DataFrame(rows)
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(buy, time="open")

def buy(context):
    order_value("600000.SH", 100000)      # 天天下单：day2 一字涨停应被拒
'''
    res = JQRunner(code, initial_cash=1_000_000).run(df)
    assert all(t.trade_date == d1 for t in res.trades)
    assert any("涨停" in r[2] for r in res.rejected)


def test_syntax_error_raises():
    with pytest.raises(ValueError, match="策略代码执行失败"):
        JQRunner("def initialize(context\n    pass", initial_cash=1e6)


def test_handle_data_two_arg_signature():
    """聚宽标准签名 handle_data(context, data)：data 是当日 bar 视图，可下单。"""
    from datetime import date, timedelta

    import polars as pl
    rows = []
    for i in range(6):
        d = date(2026, 1, 5) + timedelta(days=i)
        px = 10.0 + i
        rows.append(dict(trade_date=d, symbol="600000.SH", open=px, high=px,
                         low=px, close=px, pre_close=px - 1 if i else 9.0,
                         volume=1e6, amount=1e7))
    df = pl.DataFrame(rows)
    code = '''
def initialize(context):
    g.seen = 0

def handle_data(context, data):
    # 双参数签名：data 支持 open/close 属性访问
    assert abs(data["600000.SH"].open - data["600000.SH"].close) < 1e-9
    if context.current_dt.date() >= date(2026, 1, 7) and g.seen == 0:
        order_value("600000.SH", 50000)
        g.seen = 1
'''
    res = JQRunner(code.replace("date(2026, 1, 7)", "__import__('datetime').date(2026, 1, 7)"),
                   initial_cash=1_000_000).run(df)
    assert res.error is None, res.error
    assert len(res.trades) == 1
    assert res.trades[0].qty == 4100      # 50000 / 12.0 / 1.001 → floor 100 手


def test_get_price_layout_matches_jq():
    """get_price 单标的返回列=fields（聚宽语义）；多标的返回 (标的,字段) MultiIndex。"""
    from datetime import date, timedelta
    rows = []
    for i in range(6):
        d = date(2026, 1, 5) + timedelta(days=i)
        px = 10.0 + i
        for s in ("600000.SH", "600519.SH"):
            rows.append(dict(trade_date=d, symbol=s, open=px, high=px * 1.01,
                             low=px * 0.99, close=px, pre_close=9.0 + i,
                             volume=1e6, amount=1e7))
    df = pl.DataFrame(rows)
    code = '''
def initialize(context):
    pass

def handle_data(context, data):
    single = get_price("600000.SH", count=5, fields=["close"])
    assert list(single.columns) == ["close"], single.columns
    if len(single) < 5:
        return                                   # 历史不足，跳过
    assert single.index[-1] < context.current_dt.date().isoformat()  # 不含今日（索引为字符串日期）
    assert single["close"][-1] == 14.0           # 昨日收盘（负数下标按位置）
    multi = get_price(["600000.SH", "600519.SH"], count=3,
                      fields=["close", "open"])
    assert ("600000.SH", "close") in multi.columns
'''
    res = JQRunner(code, initial_cash=1_000_000).run(df)
    assert res.error is None, res.error


# ---------- G1 接线：security 表 is_st ----------

from lquant.data.store import catalog  # noqa: E402

duckdb = pytest.importorskip("duckdb")


@pytest.fixture()
def tmp_catalog(tmp_path, monkeypatch):
    """隔离 duckdb catalog（同 test_baseline_strategy 模式），seed security 表。"""
    from contextlib import contextmanager

    db = tmp_path / "lq.duckdb"
    con = duckdb.connect(str(db))
    from lquant.data.store.ddl import DDL_STATEMENTS
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()

    @contextmanager
    def _writer():
        c = duckdb.connect(str(db))
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @contextmanager
    def _reader():
        c = duckdb.connect(str(db))
        try:
            yield c
        finally:
            c.close()

    monkeypatch.setattr(catalog, "writer", _writer)
    monkeypatch.setattr(catalog, "reader", _reader)


def test_is_st_wired_from_security_table(tmp_catalog):
    """security 表 is_st=TRUE → get_current_data()[sym].is_st 为真（G1 接线金标准）。

    修复前 _sec_data 不传 is_st，恒 False。
    """
    with catalog.writer() as con:
        con.execute("INSERT INTO security VALUES "
                    "('600000.SH', '*ST测试', 'stock', 'main', "
                    "DATE '2020-01-01', NULL, TRUE, 'test', now())")
    code = '''
def initialize(context):
    run_daily(trade, time="open")

def trade(context):
    cd = get_current_data()
    record(st_flag=1 if cd["600000.SH"].is_st else 0)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is None, res.error
    assert res.records["st_flag"], "record 无输出"
    assert all(v == 1 for _, v in res.records["st_flag"])


def test_is_st_default_empty_when_no_security_rows(tmp_catalog):
    """security 表存在但无 ST 行 → is_st 恒 False，回测正常不报错。"""
    code = """
def initialize(context):
    run_daily(trade, time="open")

def trade(context):
    record(st_flag=1 if get_current_data()["600000.SH"].is_st else 0)
"""
    res = JQRunner(code, initial_cash=1_000_000).run(make_df())
    assert res.error is None, res.error
    assert res.records["st_flag"], "record 无输出"
    assert all(v == 0 for _, v in res.records["st_flag"])


def test_st_price_limit_5pct(tmp_catalog):
    """ST 股 +7% 高开（> 5% 涨停价）→ 每日买入委托全部涨停拒单。

    对照 test_non_st_7pct_gap_fills：同行情非 ST 可成交。
    """
    rows = []
    for i in range(4):
        d = date(2026, 1, 5 + i)
        rows.append(dict(trade_date=d, symbol="600000.SH", open=107.0,
                         high=107.0 * 1.005, low=107.0 * 0.995, close=107.0,
                         pre_close=100.0, volume=1e8, amount=107.0 * 1e8))
    df = pl.DataFrame(rows)
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(trade, time="open")

def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_target_value("600000.SH", 500000)
'''
    runner = JQRunner(code, initial_cash=1_000_000,
                      security_meta={"600000.SH": {"is_st": True}})
    res = runner.run(df)
    assert res.error is None, res.error
    assert res.trades == [], f"ST +7% 高开不应成交: {res.trades}"
    st_rejects = [r for r in res.rejected if "涨停" in r[2]]
    assert st_rejects, f"预期 ST 5% 涨停拒单，实际 {res.rejected}"


def test_non_st_7pct_gap_fills(tmp_catalog):
    """同行情非 ST 股（无 meta 注入）+7% 高开可成交 —— 10% 板内对照组。"""
    rows = []
    for i in range(4):
        d = date(2026, 1, 5 + i)
        p = 100.0 if i == 0 else 107.0
        rows.append(dict(trade_date=d, symbol="600000.SH", open=p, high=p * 1.005,
                         low=p * 0.995, close=p, pre_close=100.0,
                         volume=1e8, amount=p * 1e8))
    df = pl.DataFrame(rows)
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(trade, time="open")

def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_target_value("600000.SH", 500000)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(df)
    assert res.error is None, res.error
    assert len(res.trades) == 1, res.trades          # day0 买入后不再下单
    assert not [r for r in res.rejected if "涨停" in r[2]]
