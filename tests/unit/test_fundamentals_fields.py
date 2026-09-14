"""财务前缀映射修正的回归测试:income/balance 指向真实 tushare 前缀。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.data.store import catalog
from lquant.research.dialect.fundamentals import cashflow, income, query

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pandas")

_DAY = date(2026, 8, 1)


@pytest.fixture()
def tmp_catalog(tmp_path: Path, monkeypatch) -> None:
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


def _seed_financial(rows: list[dict]) -> None:
    df = pl.DataFrame(rows, schema_overrides={
        "stat_date": pl.Date, "pub_date": pl.Date,
        "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8,
        "ingested_at": pl.Datetime,
    })
    from lquant.data.store.catalog import FinancialRepo

    FinancialRepo().upsert(df)


def _row(symbol: str, item: str, value: float, stat: date | None = None,
         pub: date | None = None) -> dict:
    return {"symbol": symbol, "stat_date": stat or date(2026, 3, 31),
            "pub_date": pub or date(2026, 4, 25), "report_type": "2026Q1",
            "item": item, "value": value}


def test_income_net_profit_resolves_from_real_prefix(tmp_catalog):
    """修复前 income 映射 profit. 前缀(0 行)→ 空列;修复后命中 income. 行。"""
    from lquant.research.dialect.fundamentals import income, query
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([
        _row("600519.SH", "income.n_income_attr_p", 42.0),
        _row("600519.SH", "income.n_income_attr_p", 999.0,
             stat=date(2026, 6, 30), pub=date(2026, 12, 31)),
    ])
    df = get_fundamentals(query(income.net_profit), date=_DAY)
    assert df["net_profit"].tolist() == [42.0]


def test_income_native_field_direct_query(tmp_catalog):
    """tushare 原生列名可直接查:income.n_income_attr_p。"""
    from lquant.research.dialect.fundamentals import income, query
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "income.n_income_attr_p", 7.0)])
    df = get_fundamentals(query(income.n_income_attr_p), date=_DAY)
    assert df["n_income_attr_p"].tolist() == [7.0]


def test_balance_total_assets_via_balancesheet_prefix(tmp_catalog):
    """balance.total_assets 现在映射 balancesheet. 前缀(真实数据所在)。"""
    from lquant.research.dialect.fundamentals import balance, query
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "balancesheet.total_assets", 3.0)])
    df = get_fundamentals(query(balance.total_assets), date=_DAY)
    assert df["total_assets"].tolist() == [3.0]


def test_indicator_jq_field_maps_to_tushare_tail(tmp_catalog):
    """FIELD_MAP:indicator.net_profit_growth_rate → indicator.netprofit_yoy。"""
    from lquant.research.dialect.fundamentals import indicator, query
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "indicator.netprofit_yoy", 0.35)])
    df = get_fundamentals(query(indicator.net_profit_growth_rate), date=_DAY)
    assert df["net_profit_growth_rate"].tolist() == [0.35]


def test_unknown_financial_field_raises():
    from lquant.research.dialect.jq_shim import get_fundamentals

    with pytest.raises(ValueError, match="income"):
        get_fundamentals(query(income.not_a_real_field), date=_DAY)


def test_same_tail_multi_columns_all_filled(tmp_catalog):
    """Critical 回归:同查询两列映射到同一 tushare 尾段,两列都不得丢数。"""
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "income.n_income_attr_p", 42.0)])
    df = get_fundamentals(
        query(income.net_profit, income.nparent_netprofit), date=_DAY)
    assert df["net_profit"].tolist() == [42.0]
    assert df["nparent_netprofit"].tolist() == [42.0]


def test_cashflow_operate_flow_resolves(tmp_catalog):
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "cashflow.n_cashflow_act", 12.5)])
    df = get_fundamentals(query(cashflow.net_operate_cash_flow), date=_DAY)
    assert df["net_operate_cash_flow"].tolist() == [12.5]


def test_unknown_cashflow_field_raises():
    from lquant.research.dialect.jq_shim import get_fundamentals

    with pytest.raises(ValueError, match="未知字段"):
        get_fundamentals(query(cashflow.made_up_field), date=_DAY)


def test_legacy_profit_prefix_still_resolves(tmp_catalog):
    """历史 profit. 前缀数据兼容:老库不丢数。"""
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "profit.netProfit", 5.0)])
    df = get_fundamentals(query(income.net_profit), date=_DAY)
    assert df["net_profit"].tolist() == [5.0]


# ---------- Task 2: growth/operation 映射 ----------
# 真实库勘察(2026-09-14):financial_pit 无 growth./operation. 前缀行(0 行);
# indicator 前缀下存在同比尾段 or_yoy/netprofit_yoy/dt_netprofit_yoy/op_yoy/
# ebt_yoy/assets_yoy 与周转尾段 assets_turn/ar_turn/ca_turn/fa_turn。


def test_growth_net_profit_yoy_via_indicator_prefix(tmp_catalog):
    """growth.inc_net_profit_year_on_year → indicator.netprofit_yoy(手算:种子值 42.0 直通)。"""
    from lquant.research.dialect.fundamentals import growth
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "indicator.netprofit_yoy", 42.0)])
    df = get_fundamentals(
        query(growth.inc_net_profit_year_on_year), date=_DAY)
    assert df["inc_net_profit_year_on_year"].tolist() == [42.0]


def test_growth_revenue_yoy_resolves(tmp_catalog):
    """growth.inc_revenue_year_on_year → indicator.or_yoy(种子值 21.5 直通)。"""
    from lquant.research.dialect.fundamentals import growth
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "indicator.or_yoy", 21.5)])
    df = get_fundamentals(query(growth.inc_revenue_year_on_year), date=_DAY)
    assert df["inc_revenue_year_on_year"].tolist() == [21.5]


def test_operation_assets_turnover_resolves(tmp_catalog):
    """operation.total_asset_turnover_rate → indicator.assets_turn(种子值 0.8 直通)。"""
    from lquant.research.dialect.fundamentals import operation
    from lquant.research.dialect.jq_shim import get_fundamentals

    _seed_financial([_row("600519.SH", "indicator.assets_turn", 0.8)])
    df = get_fundamentals(query(operation.total_asset_turnover_rate), date=_DAY)
    assert df["total_asset_turnover_rate"].tolist() == [0.8]


def test_unknown_growth_field_raises():
    """growth 收紧为白名单:未知字段报错并列出可选字段。"""
    from lquant.research.dialect.fundamentals import growth
    from lquant.research.dialect.jq_shim import get_fundamentals

    with pytest.raises(ValueError, match="未知字段 growth"):
        get_fundamentals(query(growth.made_up_field), date=_DAY)


def test_unknown_operation_field_raises():
    from lquant.research.dialect.fundamentals import operation
    from lquant.research.dialect.jq_shim import get_fundamentals

    with pytest.raises(ValueError, match="未知字段 operation"):
        get_fundamentals(query(operation.made_up_field), date=_DAY)
