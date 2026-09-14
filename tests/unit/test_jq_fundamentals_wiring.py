"""JQRunner 沙箱接入 get_fundamentals 的接线测试。

fixture 模板抄 tests/unit/test_jq_shim_m6b.py（tmp_catalog/_seed_financial），
纯回测行情抄 tests/unit/test_jq_api.py::make_df。离线可跑。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.data.store import catalog

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pandas")


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


def _make_df():
    # 6 个交易日、两只股票；抄 tests/unit/test_jq_api.py::make_df 的构造方式
    rows = []
    px = {"600000.SH": 100.0, "000001.SZ": 50.0}
    for i in range(6):
        d = date.fromordinal(date(2026, 1, 5).toordinal() + i)
        for s, base in px.items():
            p = base * (1.02 ** i) if s.startswith("6") else base * (0.99 ** i)
            pre = p if i == 0 else (
                base * (1.02 ** (i - 1)) if s.startswith("6") else base * (0.99 ** (i - 1)))
            rows.append(dict(trade_date=d, symbol=s, open=p, high=p * 1.005, low=p * 0.995,
                             close=p, pre_close=pre, volume=1e8, amount=p * 1e8))
    return pl.DataFrame(rows)


def _seed_financial(rows: list[dict]) -> None:
    df = pl.DataFrame(rows, schema_overrides={
        "stat_date": pl.Date, "pub_date": pl.Date,
        "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8,
        "ingested_at": pl.Datetime,
    })
    catalog.FinancialRepo().upsert(df)


CODE = '''
def initialize(context):
    run_monthly(pick, time="open")

def pick(context):
    df = get_fundamentals(query(income.net_profit))
    record(n_picked=len(df))
    order_value("600000.SH", 100000)
'''


def test_strategy_can_call_get_fundamentals(tmp_catalog, tmp_path, monkeypatch):
    # 日线写临时 parquet（read_daily 按 _root 读取），抄 tmp_catalog/_bars 模板
    bars = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"] * 6,
        "trade_date": [date.fromordinal(date(2026, 1, 5).toordinal() + i)
                       for i in range(6) for _ in range(2)],
        "open": [100.0] * 12,
        "high": [101.0] * 12,
        "low": [99.0] * 12,
        "close": [100.0] * 12,
        "volume": [1e8] * 12,
        "amount": [1e10] * 12,
        "adj_factor": [1.0] * 12,
        "pe_ttm": [30.0] * 12,
        "pb_mrq": [5.0] * 12,
        "total_mv": [1e10] * 12,
        "float_mv": [8e9] * 12,
    })
    root = tmp_path / "data" / "daily" / "year=2026"
    root.mkdir(parents=True)
    bars.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")

    # 2025Q4 财务，公告于 2025-12-20，回测区间前已公开 → PIT 可见
    _seed_financial([
        {"symbol": "600000.SH", "stat_date": date(2025, 12, 31),
         "pub_date": date(2025, 12, 20), "report_type": "2025Q4",
         "item": "profit.netProfit", "value": 42.0},
    ])

    from lquant.backtest.jqapi import JQRunner

    res = JQRunner(CODE, initial_cash=1_000_000).run(_make_df())
    assert res.error is None
    assert any(v > 0 for _, v in res.records.get("n_picked", []))   # 策略内确实查到了财务行
