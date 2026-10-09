"""审阅修复补测：把改动行里尚未被回归测试覆盖的分支补齐。

对应 `lquant_review_fixes.patch` 引入的防御/边界分支 —— 行为本身已在
`test_deep_review_*` 里锁定主路径，这里只补「第一次改动没走到」的出口：

- broker 截量第二轮复查（滑点随成交量变化时，第一轮反解价失准）
- tiered 除权日棘轮止盈线同步缩放
- 涨停池翻页的 tc 终止 / 页数兜底、HHMMSS 非正值
- 看板 limit_up_pool 加列迁移（seal_amount，保历史）
- Dataset purge 窗口短于泄漏窗时训练段置空
- sync_run 表未建时 history 返回空（纯 SELECT 不建表）
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.backtest.broker import Broker
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.exit import ExitContext, PositionView, TieredExitStrategy
from lquant.backtest.rules.model import (
    Commission,
    InstrumentRules,
    PriceLimit,
    TaxSchedule,
)
from lquant.core.types import SecType, parse_symbol
from lquant.research.ml.dataset import DatasetConfig, build_dataset

# ------------------------------------------------------------------ broker


class _VolumeDecaySlippage:
    """滑点随成交量上升：单价 = 基准 + impact/qty（数量越小单价越贵）。"""

    def __init__(self, impact: float) -> None:
        self.impact = impact

    def apply(self, price: float, side: Side, *, qty: float = 0.0,
              volume: float = 0.0) -> float:
        return price + self.impact / max(qty, 1e-9)


def _no_fee_rules(sym: str = "600000.SH", lot: int = 1) -> InstrumentRules:
    sched = [(date(2000, 1, 1), date(9999, 12, 31), 0.0,
              frozenset({"buy", "sell"}))]
    return InstrumentRules(
        symbol=parse_symbol(sym), sec_type=SecType.STOCK,
        commission=Commission(rate=0.0, min=0.0),
        tax=TaxSchedule(sched), transfer_fee_rate=0.0,
        price_limit=PriceLimit(mode="by_board", values={"main": 0.10}),
        lot_size=lot, sellable_after_days=1, price_tick=0.01)


def _flat_bar(px: float) -> Bar:
    return Bar(symbol="600000.SH", trade_date=date(2026, 6, 2),
               open=px, high=px * 1.01, low=px * 0.99, close=px,
               pre_close=px, volume=1e9, amount=px * 1e9)


def test_truncate_second_round_rejects_when_still_over_budget() -> None:
    """截量后按新滑点价重算仍超预算 → 第二轮复查后整单作废。

    `_affordable_qty` 是拿「截量前的价格」反解的。滑点随数量上升时，
    数量被截小反而单价变贵，第一轮反解出的数量在新价格下依旧买不起 ——
    必须有第二轮复查并最终拒单，否则 apply_fill 会把现金打成负数。
    """
    b = Broker({"600000.SH": _no_fee_rules()},
               slippage=_VolumeDecaySlippage(50_000.0),
               insufficient_cash="truncate")
    o = Order(order_id="gap-b1", symbol="600000.SH", side=Side.BUY, qty=2000)
    fill = b.match(o, _flat_bar(1000.0), date(2026, 6, 2), cash=1_000_000.0)
    assert fill is None
    assert o.reason == "资金不足"


# ------------------------------------------------------------------ tiered


def test_tiered_trailing_stop_rescaled_on_exrights() -> None:
    """除权日（adj_factor 跳变）棘轮止盈线必须按因子比同步缩放。

    止盈线存的是绝对价（与 peak/avg_cost 同口径）。若不缩，棘轮「只升不降」
    会把它锁在除权前的尺度上，除权次日横盘即假触发全仓退出。
    """
    s = TieredExitStrategy()
    p = PositionView(symbol="600519.SH", qty=1000.0, available_qty=1000.0,
                     avg_cost=8.0)
    b1 = Bar(symbol="600519.SH", trade_date=date(2026, 3, 2), open=12.0,
             high=12.0, low=12.0, close=12.0, pre_close=12.0,
             volume=1e6, amount=1e8, adj_factor=1.0)
    s.on_bar(ExitContext(trade_date=date(2026, 3, 2), phase="close",
                         positions=[p], bars={"600519.SH": b1}))
    level_before = s._trailing_stop["600519.SH"]
    assert level_before > 0

    b2 = Bar(symbol="600519.SH", trade_date=date(2026, 3, 3), open=8.0,
             high=8.0, low=8.0, close=8.0, pre_close=8.0,
             volume=1e6, amount=1e8, adj_factor=1.5)
    s.on_bar(ExitContext(trade_date=date(2026, 3, 3), phase="close",
                         positions=[p], bars={"600519.SH": b2}))
    assert s._trailing_stop["600519.SH"] == pytest.approx(level_before / 1.5)


# --------------------------------------------------------------- 涨停池采集


def test_fetch_pool_stops_when_total_reached(monkeypatch) -> None:
    """接口 tc（总条数）已达 → 不再翻页（行数凑齐即停）。"""
    from lquant.market.collectors import limit_up as m

    calls: list[str] = []

    def fake_get(url: str):
        calls.append(url)

        class _Resp:
            def json(self) -> dict:
                return {"data": {"pool": [{"p": 1}], "tc": 1}}

        return _Resp()

    monkeypatch.setattr(m, "em_get", fake_get)
    rows = m._fetch_pool("ZT", "2026-09-11", pagesize=1)
    assert rows == [{"p": 1}]
    assert len(calls) == 1


def test_fetch_pool_stops_at_defensive_page_cap(monkeypatch) -> None:
    """tc 一直不满足时，兜底 20 页上限必须终止（防止死循环/无限抓取）。"""
    from lquant.market.collectors import limit_up as m

    calls: list[str] = []

    def fake_get(url: str):
        calls.append(url)

        class _Resp:
            def json(self) -> dict:
                return {"data": {"pool": [{"p": 1}], "tc": 10 ** 9}}

        return _Resp()

    monkeypatch.setattr(m, "em_get", fake_get)
    rows = m._fetch_pool("ZT", "2026-09-11", pagesize=1)
    assert len(rows) == 20
    assert len(calls) == 20


def test_ts_to_hhmmss_nonpositive_returns_empty() -> None:
    """fbt/lbt 非正值（含负数）视为缺失返回空串，而不是算出 1970 年时间。"""
    from lquant.market.collectors.limit_up import _ts_to_hhmmss

    assert _ts_to_hhmmss(92503) == "09:25:03"
    assert _ts_to_hhmmss(-5) == ""
    assert _ts_to_hhmmss(0) == ""


# ------------------------------------------------------- 看板表加列迁移


def test_market_schema_alters_seal_amount_on_legacy_table(tmp_path) -> None:
    """老库 limit_up_pool 缺 seal_amount → ALTER 加列而非 drop 重建（保历史）。"""
    import duckdb

    from lquant.market.schema import ensure_market_tables

    con = duckdb.connect(str(tmp_path / "market.duckdb"))
    try:
        con.execute("CREATE TABLE limit_up_pool(trade_date DATE, symbol VARCHAR)")
        ensure_market_tables(con)
        cols = {r[0] for r in con.execute("DESCRIBE limit_up_pool").fetchall()}
    finally:
        con.close()
    assert "seal_amount" in cols


# ------------------------------------------------------------ ML 数据集切分


def _ml_panel(n_days: int = 20, n_syms: int = 12) -> pl.DataFrame:
    rows = []
    for i in range(n_syms):
        for k in range(n_days):
            rows.append({"trade_date": date(2024, 1, 1) + timedelta(days=k),
                         "symbol": f"S{i:02d}", "close": 10.0 + k + 0.1 * i,
                         "mom": float(k)})
    return pl.DataFrame(rows)


def test_dataset_split_purge_window_shorter_than_leak() -> None:
    """训练段短于泄漏窗（label_horizon）→ 训练集置空，不静默放弃 purge。"""
    ds = build_dataset(_ml_panel(), DatasetConfig(features=["mom"],
                                                  label_horizon=5))
    assert len(ds.dates) >= 7
    train, valid, test = ds.split(ds.dates[2], ds.dates[6], purge=True)
    assert train.height == 0
    assert valid.height >= 0 and test.height >= 0


# ------------------------------------------------------------ sync 历史


def test_paper_store_migrate_adds_legacy_columns() -> None:
    """老 paper 库补 is_limit / last_day_close 列（CREATE IF NOT EXISTS 不加列）。"""
    import sqlite3

    from lquant.paper import store

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE paper_position(account TEXT, symbol TEXT)")
        con.execute("CREATE TABLE paper_order(account TEXT, order_id TEXT)")
        con.execute("CREATE TABLE paper_account(account TEXT)")
        store._migrate(con)
        ocols = {r[1] for r in con.execute("PRAGMA table_info(paper_order)").fetchall()}
        acols = {r[1] for r in con.execute("PRAGMA table_info(paper_account)").fetchall()}
    finally:
        con.close()
    assert "is_limit" in ocols
    assert "last_day_close" in acols


def test_paper_broker_market_slippage_and_sell_limit_pending() -> None:
    """市价单成交价带滑点；限价卖单报价未达限价时继续挂单（不是拒单）。"""
    from lquant.core.types import now_cn
    from lquant.paper import PaperConfig
    from lquant.paper.engine import PaperBroker, PaperOrder

    b = PaperBroker(PaperConfig(initial_cash=1_000_000.0, slippage_pct=0.001))
    b.submit("600000.SH", "buy", 100, 100.0, limit=False)
    b.on_quote("600000.SH", 100.0)
    buy = b.orders[0]
    assert buy.status == "filled"
    assert buy.filled_price == pytest.approx(100.1)   # 报价 + 滑点，不被限价帽吞掉

    sell = PaperOrder(order_id="p-sell", ts=now_cn(), symbol="600000.SH",
                      side="sell", qty=100, price=105.0, limit=True)
    b.orders.append(sell)
    b.on_quote("600000.SH", 104.0)
    assert sell.status == "pending"
    assert sell.filled_qty == 0


def test_sync_history_empty_when_table_missing(tmp_path, monkeypatch) -> None:
    """sync_run 表未建（空库）→ history 返回空列表，而不是抛错。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        from lquant.sync import manager

        assert manager.history(limit=5) == []
    finally:
        get_settings.cache_clear()
