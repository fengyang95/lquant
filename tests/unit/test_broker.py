from datetime import date

from lquant.backtest.broker import Broker
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.rules.model import Commission, InstrumentRules, PriceLimit, TaxSchedule
from lquant.core.types import Symbol


def _rules(per_order=True, tax=0.0, min_comm=5.0) -> InstrumentRules:
    return InstrumentRules(
        symbol=Symbol("600000", "SH"), sec_type=__import__("lquant.core.types", fromlist=["SecType"]).SecType.STOCK,
        commission=Commission(rate=0.00025, min=min_comm, per_order=per_order),
        tax=TaxSchedule([(date(2000, 1, 1), date(9999, 12, 31), tax)]),
        transfer_fee_rate=0.0,
        price_limit=PriceLimit("by_board", {"main": 0.10}),
        lot_size=100,
        sellable_after_days=1,
    )


def test_min_commission_once_per_order():
    """订单分两次成交，5 元最低佣金只应收一次（总额 = max(5, 4000*0.00025) = 5）。"""
    r = _rules(per_order=True)
    b = Broker({"600000.SH": r})
    o = Order("o1", "600000.SH", Side.BUY, 400)
    bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 10.5, 9.5, 10.0, 10.0, 1e6, 1e7)
    f1 = b.match(o, bar, date(2026, 1, 5), max_qty=200)
    f2 = b.match(o, bar, date(2026, 1, 5), max_qty=200)
    assert f1 is not None and f2 is not None
    assert f1.qty == 200 and f2.qty == 200
    assert abs((f1.fee + f2.fee) - 5.0) < 1e-6
    assert abs(f1.fee - 5.0) < 1e-6     # 第一笔补足到 5 元
    assert abs(f2.fee - 0.0) < 1e-6     # 第二笔已被覆盖，不再收


def test_lot_size_and_limit_up():
    r = _rules()
    b = Broker({"600000.SH": r})
    # 涨停开盘：不可买入
    o = Order("o2", "600000.SH", Side.BUY, 100)
    bar = Bar("600000.SH", date(2026, 1, 5), 11.0, 11.0, 10.9, 11.0, 10.0, 1e6, 1e7)
    assert b.match(o, bar, date(2026, 1, 5)) is None
    assert o.reason == "涨停不可买"


def test_etf_sell_no_stamp_tax():
    r = _rules(tax=0.0)
    b = Broker({"510300.SH": r})
    o = Order("o3", "510300.SH", Side.SELL, 10000)
    bar = Bar("510300.SH", date(2026, 1, 5), 4.0, 4.1, 3.9, 4.0, 4.0, 1e6, 1e7)
    f = b.match(o, bar, date(2026, 1, 5))
    assert f is not None
    # 40000 元 * 0.00025 = 10 元 > 最低 5 元，且无印花税
    assert abs(f.fee - 10.0) < 1e-6


def _rules_full(per_order=True, tax=0.0, min_comm=5.0, transfer=0.00001) -> InstrumentRules:
    from lquant.backtest.rules.model import TaxSchedule as TS
    return InstrumentRules(
        symbol=Symbol("600000", "SH"),
        sec_type=__import__("lquant.core.types", fromlist=["SecType"]).SecType.STOCK,
        commission=Commission(rate=0.00025, min=min_comm, per_order=per_order),
        tax=TS([(date(2000, 1, 1), date(9999, 12, 31), tax)]),
        transfer_fee_rate=transfer,
        price_limit=PriceLimit("by_board", {"main": 0.10}),
        lot_size=100,
        sellable_after_days=1,
    )


def test_sell_stamp_tax_amount():
    """卖出印花税 = 成交额 × 税率，买入无印花税。"""
    r = _rules_full(tax=0.001, transfer=0.0)
    b = Broker({"600000.SH": r})
    bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 10.5, 9.5, 10.0, 10.0, 1e6, 1e7)
    fs = b.match(Order("s1", "600000.SH", Side.SELL, 1000), bar, date(2026, 1, 5))
    assert fs is not None
    # 佣金 max(5, 10000*0.00025=2.5)=5 + 印花税 10 = 15
    assert abs(fs.fee - 15.0) < 1e-6
    fb = b.match(Order("b1", "600000.SH", Side.BUY, 1000), bar, date(2026, 1, 5))
    assert fb is not None
    assert abs(fb.fee - 5.0) < 1e-6


def test_transfer_fee_both_sides():
    """过户费买卖双向收取。"""
    r = _rules_full(tax=0.0, min_comm=0.0, transfer=0.00001)
    b = Broker({"600000.SH": r})
    bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 10.5, 9.5, 10.0, 10.0, 1e6, 1e7)
    f = b.match(Order("t1", "600000.SH", Side.BUY, 1000), bar, date(2026, 1, 5))
    # 佣金 10000*0.00025=2.5 + 过户费 10000*0.00001=0.1
    assert abs(f.fee - 2.6) < 1e-6


def test_transfer_fee_uses_trade_date_schedule():
    """Broker 按**成交日**取过户费率，而不是规则表里的现行常数。

    2022-04-29 前后各成交一笔，费率应分别为 0.00002 / 0.00001。
    佣金最低额设为 0，让费用里只剩过户费，断言可以直接对齐数字。
    """
    from lquant.backtest.rules.model import TransferFeeSchedule

    r = _rules_full(tax=0.0, min_comm=0.0)
    r.transfer_fee_schedule = TransferFeeSchedule([
        (date(2015, 8, 1), date(2022, 4, 28), 0.00002),
        (date(2022, 4, 29), date(9999, 12, 31), 0.00001),
    ])
    b = Broker({"600000.SH": r})
    bar_old = Bar("600000.SH", date(2022, 4, 28), 10.0, 10.5, 9.5, 10.0, 10.0, 1e6, 1e7)
    bar_new = Bar("600000.SH", date(2022, 4, 29), 10.0, 10.5, 9.5, 10.0, 10.0, 1e6, 1e7)
    f_old = b.match(Order("tf1", "600000.SH", Side.BUY, 1000), bar_old, date(2022, 4, 28))
    f_new = b.match(Order("tf2", "600000.SH", Side.BUY, 1000), bar_new, date(2022, 4, 29))
    # 佣金 10000*0.00025=2.5，剩下的差额就是过户费
    assert abs((f_old.fee - 2.5) - 10000 * 0.00002) < 1e-6
    assert abs((f_new.fee - 2.5) - 10000 * 0.00001) < 1e-6
    assert abs(f_old.fee - f_new.fee - 0.1) < 1e-6


def test_next_vwap_fill_price():
    """next_vwap 撮合价 = amount/volume。"""
    from lquant.backtest.slippage import NoSlippage
    r = _rules_full(min_comm=0.0)
    b = Broker({"600000.SH": r}, slippage=NoSlippage(), price_mode="next_vwap")
    # volume 1e6, amount 1.05e7 → vwap 10.5（不触及涨停，可成交）
    bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 11.5, 9.5, 11.0, 10.0, 1e6, 1.05e7)
    f = b.match(Order("v1", "600000.SH", Side.BUY, 100), bar, date(2026, 1, 5))
    assert f is not None
    assert abs(f.price - 10.5) < 1e-9


def test_limit_down_sell_rejected():
    """跌停开盘不可卖出。"""
    r = _rules_full()
    b = Broker({"600000.SH": r})
    bar = Bar("600000.SH", date(2026, 1, 5), 9.0, 9.1, 8.9, 9.0, 10.0, 1e6, 1e7)
    o = Order("d1", "600000.SH", Side.SELL, 100)
    assert b.match(o, bar, date(2026, 1, 5)) is None
    assert o.reason == "跌停不可卖"


def test_price_limit_close_check_next_close_mode():
    """next_close 模式：平开但收盘封板 → 实际成交价触界仍必须拒单。"""
    from lquant.backtest.slippage import NoSlippage
    r = _rules_full()
    b = Broker({"600000.SH": r}, slippage=NoSlippage(), price_mode="next_close")
    # 开盘 10.0 平开，收盘 11.0 涨停（pre_close 10，涨停价 11）
    bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 11.0, 9.9, 11.0, 10.0, 1e6, 1e7)
    o = Order("c1", "600000.SH", Side.BUY, 100)
    assert b.match(o, bar, date(2026, 1, 5)) is None
    assert o.reason == "涨停不可买"


def test_halted_rejected():
    r = _rules_full()
    b = Broker({"600000.SH": r})
    bar = Bar("600000.SH", date(2026, 1, 5), 10.0, 10.5, 9.5, 10.0, 10.0, 0, 0, halted=True)
    o = Order("h1", "600000.SH", Side.BUY, 100)
    assert b.match(o, bar, date(2026, 1, 5)) is None
    assert o.reason == "停牌"
