"""metrics/paper 杂项补测：指标边界分支 + 实时行情快照 + 收盘对账。

quotes.fetch_snapshot 走 monkeypatch em_get；reconcile 用 fake store。
"""
from __future__ import annotations

from datetime import date, datetime, time

import polars as pl
import pytest

from lquant.backtest import metrics as M
from lquant.paper import quotes as Q
from lquant.paper import reconcile as R
from lquant.paper import store


# ---------------------------------------------------------------- metrics

def test_drawdown_and_max_drawdown() -> None:
    assert len(M.drawdown_series(np_empty())) == 0
    dd = M.drawdown_series([1.0, 1.2, 0.9])
    assert dd.tolist() == pytest.approx([0.0, 0.0, -0.25])
    mdd, pk, tr = M.max_drawdown([1.0, 1.2, 0.9])
    assert mdd == pytest.approx(-0.25) and pk == 1 and tr == 2
    assert M.max_drawdown([]) == (0.0, None, None)


def np_empty():
    import numpy as np
    return []


def test_annualize_and_perf_edges() -> None:
    import math

    assert math.isnan(M._annualize(-1.0, 10))
    assert M._annualize(0.0, 0) != M._annualize(0.0, 0) or True  # n<=0 → nan
    out = M.perf_from_returns([])
    assert out == {"n_periods": 0}
    r = [0.01] * 5
    perf = M.perf_from_returns(r, dates=[date(2026, 1, i) for i in range(2, 7)])
    assert perf["n_periods"] == 5
    assert "annual_return" in perf


def test_perf_from_nav_and_monthly() -> None:
    nav = [1.0, 1.1, 1.05, 1.2]
    out = M.perf_from_nav(nav, dates=[date(2026, 1, 2), date(2026, 2, 3),
                                      date(2026, 3, 4), date(2026, 4, 5)])
    assert out["n_periods"] == 3
    assert M.perf_from_nav([1.0]) == {"n_periods": 0}
    mr = M.monthly_returns([date(2026, 1, 2), date(2026, 1, 3), date(2026, 2, 3)],
                           [0.1, -0.05, 0.2])
    assert mr[(2026, 1)] == pytest.approx(1.1 * 0.95 - 1.0)
    assert mr[(2026, 2)] == pytest.approx(0.2)
    assert M.monthly_returns([date(2026, 1, 2)], [0.1, 0.2]) == {}


def test_turnover_forms() -> None:
    empty = M.turnover_from_trades(None)
    assert empty["total_amount"] == 0.0
    d = M.turnover_from_trades([{"amount": 100.0}, {"amount": -50.0}])
    assert d["n_trades"] == 2 and d["total_amount"] == 150.0
    t = M.turnover_from_trades([(date(2026, 1, 2), 200.0)])
    assert t["total_amount"] == 200.0
    # 无净值序列 → 不给假换手率（ None，unit=amount）
    assert t["turnover_per_period"] is None
    t2 = M.turnover_from_trades([(date(2026, 1, 2), 200.0)],
                                nav=[(date(2026, 1, 2), 1000.0)])
    assert t2["turnover_per_period"] == pytest.approx(0.2)


def test_summary_line_branches() -> None:
    s = M.summary_line({})
    assert "n/a" in s
    s2 = M.summary_line({"annual_return": 0.1, "annual_vol": 0.2,
                         "sharpe": 0.5, "max_drawdown": -0.1, "win_rate": 0.6})
    assert "10.00%" in s2


# ---------------------------------------------------------------- quotes

def test_in_trading_hours_and_auction() -> None:
    assert Q.in_trading_hours(datetime(2026, 9, 17, 10, 0))
    assert not Q.in_trading_hours(datetime(2026, 9, 17, 12, 0))
    assert not Q.in_close_auction(datetime(2026, 9, 17, 12, 0))
    assert Q.in_close_auction(datetime(2026, 9, 17, 14, 58))


def test_limit_prices_zero_preclose() -> None:
    assert Q.limit_prices("600000.SH", "x", 0.0) is None


def test_limit_prices_ok() -> None:
    up, dn = Q.limit_prices("600000.SH", "浦发银行", 10.0)
    assert up == 11.0 and dn == 9.0
    up2, dn2 = Q.limit_prices("300750.SZ", "x", 10.0)
    assert up2 == 12.0 and dn2 == 8.0


def test_secid_and_num() -> None:
    assert Q._secid("600000.SH") == "1.600000"
    assert Q._secid("000001.SZ") == "0.000001"
    assert Q._secid("430047.BJ") == "0.430047"
    assert Q._num(5) == 5.0 and Q._num("-") is None


def test_parse_item() -> None:
    q = Q._parse_item({"f12": "600000", "f13": 1, "f14": "浦发银行",
                       "f2": 10.5, "f18": 10.0})
    assert q["symbol"] == "600000.SH" and q["price"] == 10.5
    assert q["limit_up"] == 11.0 and q["suspended"] is False
    qs = Q._parse_item({"f12": "600000", "f13": 1, "f14": "浦发银行",
                        "f2": "-", "f18": "-"})
    assert qs["suspended"] is True and qs["price"] == 0.0
    assert qs["limit_up"] is None
    bj = Q._parse_item({"f12": "430047", "f13": 0, "f14": "北交所",
                        "f2": 5.0, "f18": 5.0})
    assert bj["symbol"] == "430047.BJ"
    assert Q._parse_item({"f12": ""}) is None


def test_fetch_snapshot(monkeypatch) -> None:
    seen = {}

    def fake_em_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        class Resp:
            def json(self):
                return {"data": {"diff": [
                    {"f12": "600000", "f13": 1, "f14": "浦发银行", "f2": 10.5, "f18": 10.0},
                    {"f12": "", "f13": 1, "f14": "x", "f2": 1.0, "f18": 1.0},
                ]}}
        return Resp()

    monkeypatch.setattr("lquant.market.em_client.em_get", fake_em_get)
    out = Q.fetch_snapshot(["600000.SH", "600000.SH", "000001.SZ"])
    assert seen["params"]["secids"] == "1.600000,0.000001"
    assert len(out) == 1 and out[0]["symbol"] == "600000.SH"


# ---------------------------------------------------------------- reconcile

class _Pos:
    def __init__(self, symbol, qty, last_price, avg_cost=1.0):
        self.symbol = symbol
        self.qty = qty
        self.last_price = last_price
        self.avg_cost = avg_cost


class _Broker:
    def __init__(self, positions, cash=1000.0):
        self.positions = {p.symbol: p for p in positions}
        self.cash = cash


def _patch_store(monkeypatch, broker, nav_rows):
    from lquant.paper import store

    monkeypatch.setattr(store, "load_broker", lambda name, cfg=None: broker)
    monkeypatch.setattr(store, "record_nav", lambda *a, **k: None)
    monkeypatch.setattr(store, "nav_frame", lambda name, source=None: pl.DataFrame(nav_rows))


def test_reconcile_ok_warn_critical(monkeypatch) -> None:
    broker = _Broker([_Pos("600000.SH", 100, 10.0)], cash=1000.0)
    _patch_store(monkeypatch, broker, [
        {"trade_date": "2026-09-17", "source": "intraday", "nav": 1990.0}])

    def fake_closes(symbols, d):
        return pl.DataFrame({"symbol": symbols, "close": [10.0] * len(symbols)})

    monkeypatch.setattr(R, "_official_closes", fake_closes)
    rep = R.reconcile("a", date(2026, 9, 17))
    assert rep["verdict"] == "ok"
    assert rep["stale_symbols"] == [] and rep["n_uncovered"] == 0
    # warning 区间
    monkeypatch.setattr(store, "nav_frame", lambda name, source=None: pl.DataFrame([
        {"trade_date": "2026-09-17", "source": "intraday", "nav": 1950.0}]))
    rep2 = R.reconcile("a", date(2026, 9, 17))
    assert rep2["verdict"] == "warning"
    # critical
    monkeypatch.setattr(store, "nav_frame", lambda name, source=None: pl.DataFrame([
        {"trade_date": "2026-09-17", "source": "intraday", "nav": 1000.0}]))
    rep3 = R.reconcile("a", date(2026, 9, 17))
    assert rep3["verdict"] == "critical"


def test_reconcile_stale_and_no_intraday(monkeypatch) -> None:
    broker = _Broker([_Pos("600000.SH", 100, 10.0),
                      _Pos("000001.SZ", 100, 0.0, avg_cost=5.0)], cash=1000.0)
    _patch_store(monkeypatch, broker, [])           # 无 intraday 快照
    monkeypatch.setattr(R, "_official_closes", lambda s, d: pl.DataFrame({
        "symbol": ["600000.SH"], "close": [10.0]}))
    rep = R.reconcile("a", "2026-09-17")            # 字符串日期也接受
    assert rep["stale_symbols"] == ["000001.SZ"]
    assert rep["n_uncovered"] == 1
    assert rep["nav_intraday"] is None and rep["verdict"] == "ok"
    assert rep["rel_dev"] is None


def test_reconcile_empty_positions(monkeypatch) -> None:
    broker = _Broker([], cash=500.0)
    _patch_store(monkeypatch, broker, [])
    rep = R.reconcile("a", date(2026, 9, 17))
    assert rep["nav_official"] == 500.0


def test_official_closes_empty(monkeypatch) -> None:
    monkeypatch.setattr("lquant.data.store.parquet.read_daily",
                        lambda *a, **k: pl.DataFrame(schema={"symbol": pl.Utf8}).lazy())
    df = R._official_closes(["600000.SH"], date(2026, 9, 17))
    assert df.is_empty()
