"""模拟盘常驻链路回归：实时快照解析、持久化往返、盘中 tick、收盘对账。

全部离线：行情/日线均 monkeypatch，不打真实接口。
"""

import datetime

import polars as pl
import pytest

from lquant.paper import service, store
from lquant.paper.quotes import _parse_item, limit_prices


@pytest.fixture(autouse=True)
def _paper_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_PAPER_DB", str(tmp_path / "paper.db"))


# 盘中 tick 只在**交易日**落 intraday 净值（非交易日不落，见 paper/service.py 的
# _is_trading_day 门禁），而 today_cn() 取真实日期 —— 于是周末/节假日跑这几个
# 用例必然失败（本次就是周日，且撞上国庆假期）。把「今天」和交易日判定一起
# 钉死，用例就不再依赖运行日期。
_FROZEN_DAY = datetime.date(2026, 9, 30)


@pytest.fixture(autouse=True)
def _freeze_trading_day(monkeypatch):
    monkeypatch.setattr(service, "today_cn", lambda: _FROZEN_DAY)
    monkeypatch.setattr(service, "_is_trading_day", lambda d: True)


# ---------- 行情快照 ----------

def test_parse_item_normal():
    q = _parse_item({"f12": "600000", "f13": 1, "f14": "浦发银行",
                     "f2": 10.5, "f18": 10.0})
    assert q["symbol"] == "600000.SH"
    assert q["price"] == 10.5 and q["pre_close"] == 10.0
    assert q["limit_up"] == 11.0 and q["limit_down"] == 9.0
    assert not q["suspended"]


def test_parse_item_suspended():
    q = _parse_item({"f12": "600000", "f13": 1, "f14": "浦发银行",
                     "f2": "-", "f18": 10.0})
    assert q["suspended"] and q["price"] == 0.0
    assert q["limit_up"] == 11.0      # 停牌仍有昨收可算涨跌停


def test_limit_prices_by_board_and_st():
    assert limit_prices("600000.SH", "浦发银行", 10.0) == (11.0, 9.0)
    assert limit_prices("688001.SH", "某科创", 100.0) == (120.0, 80.0)
    assert limit_prices("300001.SZ", "某创业", 100.0) == (120.0, 80.0)
    assert limit_prices("600000.SH", "ST某", 10.0) == (10.5, 9.5)


def test_limit_prices_bj_exchange_mapping():
    q = _parse_item({"f12": "830799", "f13": 0, "f14": "北交某", "f2": 5.0, "f18": 5.0})
    assert q["symbol"] == "830799.BJ"


# ---------- 持久化 ----------

def test_store_roundtrip():
    store.create_account("a1", 1_000_000)
    broker = store.load_broker("a1")
    o = broker.submit("600000.SH", "buy", 1000, 10.0)
    broker.on_quote("600000.SH", 10.0)
    store.save_broker("a1", broker)

    again = store.load_broker("a1")
    assert again.cash < 1_000_000
    assert again.positions["600000.SH"].qty == 1000
    assert again.positions["600000.SH"].available == 0      # T+1 未解冻
    assert again.orders[0].status == "filled"
    # 单号序列跨进程连续
    o2 = again.submit("600000.SH", "sell", 100, 10.5)
    assert o2.order_id != o.order_id and o2.order_id[-1].isdigit()


def test_load_broker_missing_account():
    with pytest.raises(store.AccountNotFound):
        store.load_broker("nope")


# ---------- 盘中 tick ----------

def _patch_quotes(monkeypatch, price=10.5, suspended=False):
    from lquant.paper import quotes
    monkeypatch.setattr(quotes, "fetch_snapshot", lambda syms: [
        {"symbol": "600000.SH", "name": "浦发银行", "price": 0 if suspended else price,
         "pre_close": 10.0, "limit_up": 11.0, "limit_down": 9.0,
         "suspended": suspended}])


def test_tick_fills_pending_order_and_marks(monkeypatch):
    service.create_account("t1", 1_000_000)
    # 限价 10.5：tick 快照价 10.5 触达限价 → 成交；若价 10.8 则保持挂单
    o = service.submit_order("t1", "600000.SH", "buy", 1000, price=10.5)
    assert o["status"] == "pending"
    _patch_quotes(monkeypatch, price=10.5)
    res = service.tick("t1")
    assert res["n_quotes"] == 1
    st = service.status("t1")
    assert not st["pending_orders"]
    pos = st["positions"][0]
    assert pos["qty"] == 1000 and pos["last_price"] == 10.5    # 盯市刷新
    # intraday 净值已落库
    nav = store.nav_frame("t1", "intraday")
    assert len(nav) == 1 and nav["n_positions"][0] == 1


def test_tick_limit_buy_not_filled_above_limit(monkeypatch):
    """限价语义回归：快照价高于买限价 → 不成交，继续挂单（不是按市价吃单）。"""
    service.create_account("t1b", 1_000_000)
    service.submit_order("t1b", "600000.SH", "buy", 1000, price=10.0)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("t1b")
    st = service.status("t1b")
    assert st["pending_orders"], "高于限价必须保持挂单"
    assert st["positions"] == []


def test_tick_skips_suspended_keeps_pending(monkeypatch):
    service.create_account("t2", 1_000_000)
    service.submit_order("t2", "600000.SH", "buy", 1000, price=10.0)
    _patch_quotes(monkeypatch, suspended=True)
    res = service.tick("t2")
    assert res["n_suspended"] == 1
    st = service.status("t2")
    assert st["pending_orders"]          # 挂单保持 pending 等复牌
    assert st["positions"] == []         # 未成交不产生持仓


# ---------- 收盘对账 ----------

def _patch_daily(monkeypatch, close=10.5):
    def fake_read_daily(symbols=None, start=None, end=None):
        return pl.DataFrame({"symbol": ["600000.SH"], "close": [close]}).lazy()

    monkeypatch.setattr("lquant.data.store.parquet.read_daily", fake_read_daily)


def test_day_close_reconciles_ok(monkeypatch):
    service.create_account("t3", 1_000_000)
    service.submit_order("t3", "600000.SH", "buy", 50_000, price=10.5)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("t3")
    _patch_daily(monkeypatch, close=10.5)
    out = service.day_close("t3")   # 与 tick 同一交易日（today_cn）
    rep = out["reconcile"]
    assert rep["verdict"] == "ok"
    # T+1 已解冻
    assert service.status("t3")["positions"][0]["available"] == 50_000
    # official 净值已覆盖重算：cash + 1000 × 10.5（含滑点/费用略低）
    official = store.nav_frame("t3", "official")
    assert len(official) == 1
    cash = service.status("t3")["cash"]
    expected = cash + 50_000 * 10.5     # 官方收盘价重算
    assert abs(official["nav"][0] - expected) < 1.0


def test_reconcile_flags_divergence(monkeypatch):
    service.create_account("t4", 1_000_000)
    service.submit_order("t4", "600000.SH", "buy", 50_000, price=10.5)
    _patch_quotes(monkeypatch, price=10.5)
    service.tick("t4")
    _patch_daily(monkeypatch, close=5.0)     # 官方价与盯市价严重背离
    rep = service.day_close("t4")["reconcile"]
    assert rep["verdict"] == "critical"


# ---------- 撤单与策略白名单 ----------

def test_cancel_pending_order(monkeypatch):
    service.create_account("t5", 1_000_000)
    o = service.submit_order("t5", "600000.SH", "buy", 1000, price=10.0)
    out = service.cancel_order("t5", o["order_id"])
    assert out["status"] == "cancelled"
    assert service.status("t5")["pending_orders"] == []


def test_resolve_strategy_rejects_non_whitelisted_module():
    """HTTP 入口的 import 边界：白名单外模块直接拒绝，不进 importlib。"""
    with pytest.raises(ValueError, match="白名单"):
        service.resolve_strategy("os:system")


def test_resolve_strategy_allows_lquant_module(monkeypatch):
    inst = service.resolve_strategy("lquant.paper.service:_Manual")
    assert callable(getattr(inst, "signals", None))
    with pytest.raises(ValueError, match="白名单"):
        service.resolve_strategy("subprocess:Popen")
