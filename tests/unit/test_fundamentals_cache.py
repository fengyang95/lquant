"""G13:get_fundamentals 按日 resolve 缓存(全市场单次 ~170s → 命中后毫秒级)。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from lquant.data.store import catalog

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pandas")

_D1 = date(2026, 8, 3)
_D2 = date(2026, 8, 4)


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
    # 估值列走真实日线湖（read_daily），本组单测只验缓存键、不依赖真实湖：
    # 显式置空，避免 pre-push 在新 worktree 兜底生成的 demo 湖（无 pe_ttm 列）
    # 让 _valuation_frame 的 select 抛 ColumnNotFoundError。
    monkeypatch.setattr("lquant.data.store.parquet.lake_is_empty",
                        lambda *a, **k: True)


class _Counter:
    """对模块级取数函数计数包装(计数器独立于被包装对象)。"""

    def __init__(self, fn) -> None:
        self._fn = fn
        self.n = 0

    def __call__(self, *args, **kwargs):
        self.n += 1
        return self._fn(*args, **kwargs)


@pytest.fixture()
def counted_frames(monkeypatch):
    from lquant.research.dialect import fundamentals as fd

    fd.clear_fundamentals_cache()
    fin = _Counter(fd._financial_frame)
    val = _Counter(fd._valuation_frame)
    monkeypatch.setattr(fd, "_financial_frame", fin)
    monkeypatch.setattr(fd, "_valuation_frame", val)
    return fin, val


def _seed_financial(symbol: str, value: float) -> None:
    df = pl.DataFrame([{
        "symbol": symbol, "stat_date": date(2026, 3, 31),
        "pub_date": date(2026, 4, 25), "report_type": "2026Q1",
        "item": "income.n_income_attr_p", "value": value,
    }], schema_overrides={
        "stat_date": pl.Date, "pub_date": pl.Date,
        "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8,
        "ingested_at": pl.Datetime,
    })
    from lquant.data.store.catalog import FinancialRepo

    FinancialRepo().upsert(df)


def test_same_day_same_query_hits_cache(tmp_catalog, counted_frames):
    from lquant.research.dialect import fundamentals as fd

    fin, _val = counted_frames
    q = fd.query(fd.income.net_profit)
    r1 = fd.resolve(q, _D1)
    r2 = fd.resolve(q, _D1)
    assert fin.n == 1, "同日同查询第二次应命中缓存"
    assert r1.equals(r2)


def test_cross_day_recomputes(tmp_catalog, counted_frames):
    from lquant.research.dialect import fundamentals as fd

    fin, _val = counted_frames
    q = fd.query(fd.income.net_profit)
    fd.resolve(q, _D1)
    fd.resolve(q, _D2)
    assert fin.n == 2, "跨日必须重新取数(PIT 语义)"


def test_different_query_same_day_recomputes(tmp_catalog, counted_frames):
    from lquant.research.dialect import fundamentals as fd

    _fin, val = counted_frames
    fd.resolve(fd.query(fd.valuation.pe_ratio), _D1)
    fd.resolve(fd.query(fd.valuation.pb_ratio), _D1)
    assert val.n == 2, "不同查询(不同列)不得互相污染缓存"


def test_clear_fundamentals_cache(tmp_catalog, counted_frames):
    from lquant.research.dialect import fundamentals as fd

    fin, _val = counted_frames
    q = fd.query(fd.income.net_profit)
    fd.resolve(q, _D1)
    fd.clear_fundamentals_cache()
    fd.resolve(q, _D1)
    assert fin.n == 2, "clear 后应重新取数"
