"""模拟盘回归：撮合、T+N 解冻、涨跌停拒单、对拍检测。"""
from datetime import date

import polars as pl
import pytest

from lquant.paper import PaperConfig, PaperEngine, compare_nav


class _OneShotBuy:
    """每只标的首条行情各自等额买入（价格取自身行情，限价语义才成立）。

    限价单语义修正后，用 A 的行情价给 B 下限价单、而 B 的市场价更高，
    会被正确地挂在限价之下不成交 —— 测试夹具必须尊重这个语义。
    """

    def __init__(self, syms: list[str]) -> None:
        self.syms = syms
        self.done: set[str] = set()

    def signals(self, broker, quote: dict) -> list[dict]:
        s = quote["symbol"]
        if s not in self.syms or s in self.done:
            return []
        self.done.add(s)
        q = int(broker.cash / len(self.syms) / (quote["price"] * 1.01)) // 100 * 100
        if q <= 0:
            return []
        return [{"symbol": s, "side": "buy", "qty": q, "price": quote["price"]}]


def _daily_df(days: int = 5) -> pl.DataFrame:
    import datetime as dt

    d0 = date(2026, 1, 5)
    rows = []
    for k in range(days):
        d = d0 + dt.timedelta(days=k)
        for i, px in enumerate([100.0, 200.0]):
            rows.append({"trade_date": d, "symbol": f"60000{i}.SH",
                         "close": px * (1 + 0.001 * k)})
    return pl.DataFrame(rows)


def test_buy_fees_and_t1_unfreeze():
    eng = PaperEngine(_OneShotBuy(["600000.SH", "600001.SH"]),
                      PaperConfig(initial_cash=1_000_000))
    res = eng.replay(_daily_df(3))
    assert res["n_filled"] == 2 and res["n_rejected"] == 0
    # 买入后当日 available=0（T+1），日终解冻后 == qty
    pf = eng.broker.positions_frame()
    assert (pf["available"] == pf["qty"]).all()
    # 手续费真实扣掉：现金 = 本金 − 成交金额 − 费用
    cost = sum(o.filled_qty * o.filled_price
               for o in eng.broker.orders if o.status == "filled")
    assert cost > 0
    assert eng.broker.cash < 1_000_000 - cost, "必须扣了手续费"

    # 净值 = 现金 + 持仓盯市市值。不能再断言 nav < 本金：持仓必须按**最新行情**
    # 盯市（last_price 随行情刷新），行情上行时净值和必然高于本金 ——
    # 旧断言只在「持仓永远按成交价估值」的错口径下成立。
    market = sum(p.qty * p.last_price for p in eng.broker.positions.values())
    assert eng.broker.nav() == pytest.approx(eng.broker.cash + market, rel=1e-12)
    assert any(p.last_price != p.avg_cost for p in eng.broker.positions.values()), \
        "持仓应被盯市：last_price 随行情刷新，而不是停在成交价"


def test_reject_on_insufficient_cash():
    class _Oversize(_OneShotBuy):
        def signals(self, broker, quote):
            return [{"symbol": self.syms[0], "side": "buy", "qty": 10 ** 9,
                     "price": quote["price"]}]

    eng = PaperEngine(_Oversize(["600000.SH"]), PaperConfig())
    eng.push({"symbol": "600000.SH", "price": 100.0})
    assert eng.broker.orders[0].status == "rejected"
    assert "资金不足" in eng.broker.orders[0].reason


def test_reject_on_limit_up():
    class _Always(_OneShotBuy):
        def signals(self, broker, quote):
            return [{"symbol": self.syms[0], "side": "buy", "qty": 100,
                     "price": quote["price"]}]

    eng = PaperEngine(_Always(["600000.SH"]), PaperConfig())
    eng.push({"symbol": "600000.SH", "price": 110.0, "limit_up": 110.0})
    assert eng.broker.orders[0].status == "rejected"
    assert "涨停" in eng.broker.orders[0].reason


def test_compare_nav_self_is_ok():
    nav = pl.DataFrame({"trade_date": [date(2026, 1, 5), date(2026, 1, 6)],
                        "nav": [1.0, 1.01]})
    assert compare_nav(nav, nav).verdict == "ok"


def test_compare_nav_flags_divergence():
    a = pl.DataFrame({"trade_date": [date(2026, 1, 5), date(2026, 1, 6)],
                      "nav": [1_000_000.0, 1_100_000.0]})
    b = pl.DataFrame({"trade_date": [date(2026, 1, 5), date(2026, 1, 6)],
                      "nav": [1_000_000.0, 1_000_001.0]})
    rep = compare_nav(a, b)
    assert rep.verdict == "critical"
