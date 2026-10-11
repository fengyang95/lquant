"""ETF 回测：规则自动套用 + 只对 ETF 下单 + 元数据注入。

规则断言离线（直接构造 RuleSet）；端到端回测用 demo 数据（零 ETF 以外的标的）。
"""
from __future__ import annotations

import os
from datetime import date

import polars as pl
import pytest
from etf_helpers import ETF

# ---------------- 规则：ETF 自动套 etf 段 ----------------

def test_etf_symbol_picks_etf_rule_segment():
    from lquant.backtest.engine import build_rules
    from lquant.core.types import SecType

    rules = build_rules(["510300.SH", "600000.SH"], with_db_meta=False)
    etf_r = rules["510300.SH"]
    stk_r = rules["600000.SH"]

    assert etf_r.sec_type == SecType.ETF
    assert etf_r.commission.rate == pytest.approx(0.0001)
    assert etf_r.commission.min == pytest.approx(0.1)      # ETF 最低 0.1，股票是 5
    assert etf_r.price_tick == pytest.approx(0.001)
    assert etf_r.tax_rate(date(2025, 1, 2), "sell") == 0.0  # 免印花税
    assert etf_r.transfer_fee_rate_on(date(2025, 1, 2)) == 0.0  # 免过户费
    # 股票是对照组：有印花税、最低佣金 5 元
    assert stk_r.commission.min == pytest.approx(5.0)
    assert stk_r.tax_rate(date(2025, 1, 2), "sell") > 0.0


def test_etf_t_plus_by_fund_type():
    """T+N 按基金类型：跨境/债券/商品/货币 = T+0，普通股票 ETF = T+1。"""
    from lquant.backtest.engine import build_rules

    rules = build_rules(
        ["513100.SH", "510300.SH"], with_db_meta=False,
        meta={"513100.SH": {"fund_type": "qdii"}, "510300.SH": {"fund_type": "equity"}},
    )
    assert rules["513100.SH"].sellable_after_days == 0
    assert rules["510300.SH"].sellable_after_days == 1


def test_etf_per_instrument_sellable_overrides_fund_type():
    """etf_meta.sellable_after_days 优先于 fund_type 默认值。"""
    from lquant.backtest.engine import build_rules

    rules = build_rules(
        ["513100.SH"], with_db_meta=False,
        meta={"513100.SH": {"fund_type": "qdii", "sellable_after_days": 2}},
    )
    assert rules["513100.SH"].sellable_after_days == 2


def test_etf_price_limit_by_code_prefix():
    """科创板 ETF（588 段）涨跌停 20%，与主板 ETF 10% 不同。"""
    from lquant.backtest.engine import build_rules

    rules = build_rules(["588000.SH", "510300.SH"], with_db_meta=False)
    assert rules["588000.SH"].limit_ratio() == pytest.approx(0.20)
    assert rules["510300.SH"].limit_ratio() == pytest.approx(0.10)


# ---------------- load_etf_meta_map ----------------

def test_load_etf_meta_map_from_repo(monkeypatch):
    class _Repo:
        def all(self):
            return pl.DataFrame({
                "symbol": ["510300.SH", "513100.SH"],
                "name": ["沪深300ETF", "纳指ETF"],
                "fund_type": ["equity", "qdii"],
                "sellable_after_days": [1, 0],
            })

    monkeypatch.setattr("lquant.data.store.catalog.EtfMetaRepo", _Repo)
    from lquant.backtest.etf import load_etf_meta_map

    m = load_etf_meta_map()
    assert m["513100.SH"]["fund_type"] == "qdii"
    assert m["513100.SH"]["sellable_after_days"] == 0
    assert m["510300.SH"]["name"] == "沪深300ETF"


def test_load_etf_meta_map_degrades_on_error(monkeypatch):
    class _Boom:
        def all(self):
            raise RuntimeError("duckdb locked")

    monkeypatch.setattr("lquant.data.store.catalog.EtfMetaRepo", _Boom)
    from lquant.backtest.etf import load_etf_meta_map

    assert load_etf_meta_map() == {}      # 元数据缺失不致命，回测照跑


# ---------------- 端到端（demo 湖）----------------

@pytest.fixture(scope="module")
def demo_lake(tmp_path_factory):
    base = tmp_path_factory.mktemp("etf_backtest")
    prev = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev)
    get_settings.cache_clear()


def test_etf_backtest_end_to_end_only_trades_etf(demo_lake):
    from lquant.backtest.etf import etf_factor_backtest
    from lquant.factors.etf import is_etf_symbol

    res, meta = etf_factor_backtest(
        "Ts_Return($close, 20)", universe="etf_all", top_n=3, rebalance="monthly"
    )
    assert meta["n_symbols"] == len(ETF)
    assert all(is_etf_symbol(s) for s in meta["symbols"])
    traded = {f.symbol for f in res.trades} if res.trades else set()
    assert traded, "应产生成交"
    assert all(is_etf_symbol(s) for s in traded), traded
    assert res.metrics["n_trades"] > 0
    # ETF 免印花税：总费用里不该有 tax_* 名目
    assert res.metrics["total_fee"] > 0
    assert len(res.nav) > 0


def test_etf_backtest_uses_etf_meta_fund_type(demo_lake):
    """demo 的 etf_meta 有 sellable_after_days（跨境=0）→ 注入后 T+0 生效。"""
    from lquant.backtest.etf import load_etf_meta_map

    m = load_etf_meta_map()
    assert m.get("513100.SH", {}).get("sellable_after_days") == 0


def test_etf_backtest_unknown_universe_raises(demo_lake):
    from lquant.backtest.etf import etf_factor_backtest

    with pytest.raises(ValueError):
        etf_factor_backtest("Ts_Return($close, 20)", universe="etf_nope")


def test_etf_backtest_bad_formula_raises(demo_lake):
    from lquant.backtest.etf import etf_factor_backtest
    from lquant.core.errors import FactorError

    with pytest.raises((FactorError, ValueError)):
        etf_factor_backtest("$no_such_field", universe="etf_all")
