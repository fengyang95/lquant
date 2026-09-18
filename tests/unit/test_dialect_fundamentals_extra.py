"""fundamentals/jq_shim 补测：DSL 校验错误路径 + code 列解析 + 缓存淘汰。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from lquant.data.store import catalog
from lquant.research.dialect import fundamentals as fd
from lquant.research.dialect import jq_shim
from lquant.research.dialect.fundamentals import (
    fundamentals,
    income,
    query,
    valuation,
)

_DAY = date(2026, 6, 15)


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


@pytest.fixture()
def _bars(tmp_path: Path, monkeypatch):
    df = pl.DataFrame({
        "symbol": ["600519.SH"] * 3,
        "trade_date": [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)],
        "open": [10.0, 10.5, 11.0],
        "high": [10.5, 11.0, 11.5],
        "low": [9.8, 10.2, 10.7],
        "close": [10.2, 10.8, 11.2],
        "volume": [1_000_000.0] * 3,
        "amount": [1e7] * 3,
        "turnover_rate": [1.5, 1.6, 1.7],
        "pe_ttm": [30.0, 31.0, 32.0],
    })
    root = tmp_path / "data" / "daily" / "year=2026"
    root.mkdir(parents=True)
    df.write_parquet(root / "part-0.parquet")
    monkeypatch.setattr("lquant.data.store.parquet._root", lambda: tmp_path / "data")
    return df


@pytest.fixture()
def _ctx(_bars):
    jq_shim.bind(jq_shim.JQContext(engine=None, trade_date=_DAY,
                                   universe=["600519.SH"]))
    yield
    jq_shim.bind(None)


def _seed_financial(rows: list[dict]) -> None:
    df = pl.DataFrame(rows, schema_overrides={
        "stat_date": pl.Date, "pub_date": pl.Date,
        "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8,
        "ingested_at": pl.Datetime,
    })
    catalog.FinancialRepo().upsert(df)


# ---------------------------------------------------------------- DSL 校验

def test_query_empty_raises() -> None:
    with pytest.raises(ValueError, match="至少需要"):
        query()


def test_query_non_column_raises() -> None:
    with pytest.raises(ValueError, match="只接受字段对象"):
        query("net_profit")


def test_query_reserved_name_raises() -> None:
    with pytest.raises(ValueError, match="保留列名"):
        query(income.day)


def test_filter_non_code_raises() -> None:
    q = query(income.net_profit).filter(income.net_profit.in_(["1.0"]))
    with pytest.raises(ValueError, match="仅支持按 code"):
        fd.resolve(q, _DAY)


def test_unsupported_fundamentals_field_raises() -> None:
    q = query(fundamentals.unknown_field)
    with pytest.raises(ValueError):
        fd.resolve(q, _DAY)


def test_unsupported_valuation_field_raises(_bars) -> None:
    """源码缺陷：valuation 未知字段的友好报错（_resolve_column:290）不可达 ——
    _valuation_frame 取 _VALUATION_MAP[c.name] 时先 KeyError。"""
    q = query(valuation.unknown_ratio)
    with pytest.raises(ValueError, match="未支持"):
        fd.resolve(q, _DAY)  # 友好报错已可达（原来先 KeyError）


def test_unsupported_table_raises(_bars) -> None:
    from lquant.research.dialect.fundamentals import Column

    c = Column("nope", "x")
    q = fd.Query(cols=(c,))
    with pytest.raises(ValueError, match="未支持"):
        fd.resolve(q, _DAY)


# ---------------------------------------------------------------- code 列

def test_code_column_resolution(tmp_catalog, _bars) -> None:
    _seed_financial([
        dict(symbol="600519.SH", item="income.n_income_attr_p",
             stat_date=date(2025, 12, 31), pub_date=date(2026, 4, 20),
             value=100.0, unit=None, source="tushare",
             ingested_at=__import__("datetime").datetime(2026, 4, 20)),
    ])
    q = query(income.code, income.net_profit).filter(
        income.code.in_(["600519.SH"]))
    df = fd.resolve(q, _DAY)
    assert df["code"].to_list() == ["600519.SH"]
    assert df["net_profit"][0] == 100.0


def test_code_column_with_symbol_scope_only(tmp_catalog, _bars) -> None:
    # 无任何数据行：code 列仍由显式 scope 填充
    q = query(income.code, income.net_profit).filter(
        income.code.in_(["600519.SH"]))
    df = fd.resolve(q, _DAY)
    assert df["code"].to_list() == ["600519.SH"]
    assert df["net_profit"][0] is None


# ---------------------------------------------------------------- 缓存

def test_resolve_cache_hit_and_fifo_eviction(tmp_catalog, _bars, monkeypatch) -> None:
    q = query(income.net_profit)
    d1 = fd.resolve(q, _DAY)
    assert fd.resolve(q, _DAY) is d1            # 命中缓存（同对象）
    monkeypatch.setattr(fd, "_RESOLVE_CACHE_MAX", 1)
    fd.resolve(q, date(2026, 6, 14))            # 触发 FIFO 淘汰
    assert len(fd._RESOLVE_CACHE) <= 1


# ---------------------------------------------------------------- jq_shim 补

def test_shim_ctx_unbound_raises() -> None:
    jq_shim.bind(None)
    with pytest.raises(RuntimeError, match="未初始化"):
        jq_shim._ctx()


def test_shim_attribute_history(_ctx) -> None:
    out = jq_shim.attribute_history("600519.SH", 2, fields=("open", "close"))
    assert len(out) == 2
    assert out["close"].to_list() == [10.8, 11.2]


def test_shim_attribute_history_polars(_ctx) -> None:
    out = jq_shim.attribute_history("600519.SH", 1, df=False)
    assert isinstance(out, pl.DataFrame)


def test_shim_history_skip_paused_and_unknown_field(_ctx) -> None:
    out = jq_shim.history(2, field="close", security_list=["600519.SH"],
                          skip_paused=True)
    assert out["600519.SH"].to_list() == [10.8, 11.2]
    with pytest.raises(ValueError, match="未支持"):
        jq_shim.history(1, field="nope", security_list=["600519.SH"])


def test_shim_history_rejects_unit_and_df(_ctx) -> None:
    with pytest.raises(ValueError, match="仅支持日频"):
        jq_shim.history(1, unit="5m")
    with pytest.raises(ValueError, match="仅支持 df=True"):
        jq_shim.history(1, df=False)


def test_shim_history_no_pandas_fallback(_ctx, monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pandas", None)
    out = jq_shim.history(2, field="close", security_list=["600519.SH"])
    assert out == {"600519.SH": [10.8, 11.2]}
    out2 = jq_shim.history(2, field=["open", "close"],
                           security_list=["600519.SH"])
    assert out2["close"] == [10.8, 11.2]
    out3 = jq_shim.history(2, field=["open", "close"],
                           security_list=["600519.SH", "000001.SZ"])
    assert set(out3) == {"600519.SH", "000001.SZ"}


def test_shim_history_empty_lake_raises(_ctx) -> None:
    from lquant.data.store import parquet as pq

    monkey_empty = pq.lake_is_empty
    # 湖非空但窗口内无该标的 → 第二种错误
    with pytest.raises(ValueError, match="无日线数据"):
        jq_shim.history(2, field="close", security_list=["000001.SZ"])


def test_shim_get_fundamentals_requires_query(_ctx) -> None:
    with pytest.raises(ValueError, match="query"):
        jq_shim.get_fundamentals("not-a-query")


def test_shim_get_fundamentals_date_str(_ctx) -> None:
    q = query(valuation.pe_ratio)
    df = jq_shim.get_fundamentals(q, date="2026-06-12")
    assert df["pe_ratio"].to_list() == [32.0]


def test_shim_get_fundamentals_no_pandas_module(_ctx, monkeypatch) -> None:
    import importlib.util

    q = query(valuation.pe_ratio)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    out = jq_shim.get_fundamentals(q)
    assert isinstance(out, pl.DataFrame)


def test_shim_order_functions(_ctx) -> None:
    calls = []

    class Engine:
        def order_target_value(self, s, v):
            calls.append(("otv", s, v))

        def order_target_percent(self, s, p):
            calls.append(("otp", s, p))

        def order(self, s, a):
            calls.append(("o", s, a))

    ctx = jq_shim.JQContext(engine=Engine(), trade_date=_DAY, universe=[])
    jq_shim.bind(ctx)
    jq_shim.order_target_value("600519.SH", 1.0)
    jq_shim.order_target_percent("600519.SH", 0.5)
    jq_shim.order("600519.SH", 100)
    assert calls == [("otv", "600519.SH", 1.0), ("otp", "600519.SH", 0.5),
                     ("o", "600519.SH", 100)]


def test_shim_get_all_securities(_ctx, monkeypatch) -> None:
    class FakeRepo:
        def active_symbols(self):
            return ["600519.SH"]

    monkeypatch.setattr(catalog, "SecurityRepo", FakeRepo)
    df = jq_shim.get_all_securities()
    assert df["symbol"].to_list() == ["600519.SH"]


def test_shim_get_index_stocks(_ctx, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self):
            pass

        def symbols_as_of(self, code, day):
            calls.append((code, day))
            return ["600519.SH"]

    calls = []
    monkeypatch.setattr(catalog, "IndexConsRepo", FakeRepo)
    assert jq_shim.get_index_stocks("000300.XSHG") == ["600519.SH"]
    assert calls[0][0] == "000300.SH"
    assert jq_shim.get_index_stocks("custom_idx", date=date(2026, 1, 1)) == [
        "600519.SH"]
    assert calls[1][0] == "custom_idx" and calls[1][1] == date(2026, 1, 1)


def test_shim_get_trade_days(_ctx, monkeypatch) -> None:
    monkeypatch.setattr("lquant.core.calendar.trade_days",
                        lambda s, e: ["2026-06-15"])
    assert jq_shim.get_trade_days("2026-01-01", "2026-12-31") == ["2026-06-15"]


def test_shim_log(capsys) -> None:
    from loguru import logger

    logger.remove()
    logger.add(lambda m: None)
    jq_shim.log("hello", 1)                     # 不抛即可
