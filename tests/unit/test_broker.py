from datetime import date

from lquant.backtest.account import Account
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
