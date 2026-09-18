"""批次一覆盖补充：data/store/catalog.py 各 Repo 读写（tmp DuckDB，不连真实库）。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(fake_settings):
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for ddl in DDL_STATEMENTS:
            con.execute(ddl)
    return None


def _cal_df():
    return pl.DataFrame({
        "trade_date": [date(2024, 1, 2), date(2024, 1, 3)],
        "is_open": [True, False],
        "exchange": ["SSE", "SSE"],
    })


# ---------- _upsert 基础语义 ----------


def test_upsert_empty_df(db):
    from lquant.data.store.catalog import TradeCalendarRepo

    assert TradeCalendarRepo().upsert(pl.DataFrame()) == 0


def test_upsert_missing_table_raises(db, tmp_path, monkeypatch):
    """表缺列描述为空 → RuntimeError（_columns 打桩模拟空描述）。"""
    from lquant.core.db import writer
    from lquant.data.store import catalog as cat

    with writer() as con:
        con.execute("CREATE TABLE tmp_x (a VARCHAR)")

    def _no_cols(con, table):
        return []

    monkeypatch.setattr(cat, "_columns", _no_cols)
    df = pl.DataFrame({"a": ["1"]})
    with pytest.raises(RuntimeError, match="不存在"):
        cat.upsert("tmp_x", df)


def test_upsert_null_projection_for_missing_cols(db):
    """上游少列 → 补 NULL；多列 → 丢弃。"""
    from lquant.data.store.catalog import upsert

    df = pl.DataFrame({"trade_date": [date(2024, 1, 2)], "extra": ["x"]})
    assert upsert("trade_calendar", df) == 1
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute("SELECT trade_date, is_open FROM trade_calendar").fetchall()
    assert rows[0][1] is None


def test_upsert_no_pk_without_epoch_cols_appends(db):
    """无 PK 无 epoch 键 → 纯追加（重复执行会重复行 —— 调用方语义保证）。"""
    from lquant.core.db import reader, writer
    from lquant.data.store.catalog import upsert

    df = pl.DataFrame({"v": [1]})
    with writer() as con:
        con.execute("CREATE TABLE tmp_app (v INTEGER)")
    upsert("tmp_app", df)
    upsert("tmp_app", df)
    with reader() as con:
        assert con.execute("SELECT count(*) FROM tmp_app").fetchone()[0] == 2


# ---------- TradeCalendarRepo ----------


def test_calendar_repo(db):
    from lquant.data.store.catalog import TradeCalendarRepo

    repo = TradeCalendarRepo()
    repo.upsert(_cal_df())
    assert repo.is_trading_day(date(2024, 1, 2)) is True
    assert repo.is_trading_day(date(2024, 1, 3)) is False
    assert repo.is_trading_day(date(2024, 1, 4)) is False  # 无记录
    assert repo.range(date(2024, 1, 1), date(2024, 1, 4)) == [date(2024, 1, 2)]
    assert repo.count() == 2


# ---------- SecurityRepo ----------


def _seed_security_rows() -> None:
    from lquant.data.store.catalog import SecurityRepo

    SecurityRepo().upsert(pl.DataFrame({
        "symbol": ["000001.SH", "000001.SZ", "510300.SH", "160323.SZ"],
        "sec_type": ["index", "stock", "etf", "lof"],
        "list_date": [None, date(2000, 1, 1), date(2004, 1, 1), None],
        "delist_date": [None, None, None, None],
    }))


def test_security_repo_active_and_types(db):
    from lquant.data.store.catalog import SecurityRepo

    _seed_security_rows()
    repo = SecurityRepo()
    assert repo.count() == 4
    assert repo.all_symbols() == sorted(repo.all_symbols())
    assert repo.active_symbols() == ["000001.SH", "000001.SZ", "160323.SZ", "510300.SH"]
    assert repo.active_symbols(exclude_index=True) == ["000001.SZ", "160323.SZ", "510300.SH"]
    assert repo.stock_symbols() == ["000001.SZ"]
    assert repo.etf_symbols() == ["160323.SZ", "510300.SH"]
    assert repo.pending_details() == ["160323.SZ"]
    assert repo.pending_details(limit=1) == ["160323.SZ"]


def test_security_stock_symbols_exclude_delisted(db):
    from lquant.data.store.catalog import SecurityRepo

    SecurityRepo().upsert(pl.DataFrame({
        "symbol": ["A1.SZ", "A2.SZ"],
        "sec_type": ["stock", "stock"],
        "delist_date": [None, date(2020, 1, 1)],
    }))
    repo = SecurityRepo()
    assert repo.stock_symbols() == ["A1.SZ", "A2.SZ"]
    assert repo.stock_symbols(include_delisted=False) == ["A1.SZ"]


def test_security_delist_not_excluded_when_future(db):
    """退市日在未来 → 仍算在市。"""
    from lquant.data.store.catalog import SecurityRepo

    SecurityRepo().upsert(pl.DataFrame({
        "symbol": ["A9.SZ"],
        "sec_type": ["stock"],
        "delist_date": [date(9999, 1, 1)],
    }))
    assert "A9.SZ" in SecurityRepo().active_symbols()


# ---------- EtfMetaRepo ----------


def test_etf_meta_repo(db):
    from lquant.data.store.catalog import EtfMetaRepo

    repo = EtfMetaRepo()
    repo.upsert(pl.DataFrame({
        "symbol": ["510300.SH"],
        "name": ["沪深300ETF"],
        "sellable_after_days": [2],
    }))
    all = repo.all()
    assert all["sellable_after_days"][0] == 2
    assert repo.sellable_days("510300.SH") == 2
    assert repo.sellable_days("NOPE.SH") == 1  # 查无 → 默认 1


def test_etf_meta_sellable_null_defaults_one(db):
    from lquant.data.store.catalog import EtfMetaRepo

    EtfMetaRepo().upsert(pl.DataFrame({"symbol": ["510500.SH"]}))
    assert EtfMetaRepo().sellable_days("510500.SH") == 1  # NULL → 1


# ---------- FinancialRepo / FactorDefRepo ----------


def test_financial_repo(db):
    from lquant.data.store.catalog import FinancialRepo

    repo = FinancialRepo()
    repo.upsert(pl.DataFrame({
        "symbol": ["600000.SH"], "stat_date": [date(2024, 3, 31)],
        "item": ["net_profit"], "value": [1.0],
    }))
    assert repo.count() == 1


def test_factor_def_repo(db):
    from lquant.data.store.catalog import FactorDefRepo

    repo = FactorDefRepo()
    repo.upsert(pl.DataFrame({
        "name": ["mom20"], "expression": ["(close / close.shift(20)) - 1"],
        "enabled": [True],
    }))
    df = repo.all()
    assert df["name"][0] == "mom20"
    repo.upsert(pl.DataFrame({"name": ["mom20"], "enabled": [False]}))
    assert len(FactorDefRepo().all()) == 0  # enabled 过滤


# ---------- IndustryClassifyRepo ----------


def test_industry_repo_as_of_and_latest(db):
    from lquant.data.store.catalog import IndustryClassifyRepo

    repo = IndustryClassifyRepo()
    repo.upsert(pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
        "std": ["银行", "非银", "银行"],
        "std_date": [date(2010, 1, 1), date(2020, 1, 1), date(2015, 1, 1)],
    }))
    assert repo.count() == 3
    assert repo.as_of(date(2015, 6, 1)) == {"600000.SH": "银行", "000001.SZ": "银行"}
    assert repo.as_of(date(2005, 1, 1)) == {}
    assert repo.latest() == {"600000.SH": "非银", "000001.SZ": "银行"}


# ---------- IndexConsRepo 快照替换 ----------


def test_index_cons_snapshot_replacement(db):
    from lquant.data.store.catalog import IndexConsRepo

    repo = IndexConsRepo()
    repo.upsert(pl.DataFrame({
        "index_code": ["000300.SH"] * 3,
        "symbol": ["A", "B", "C"],
        "weight": [1.0, 2.0, 3.0],
        "eff_date": [date(2024, 6, 1)] * 3,
    }))
    # 同批次重投 → 整组替换，不留僵尸
    repo.upsert(pl.DataFrame({
        "index_code": ["000300.SH"] * 2,
        "symbol": ["A", "D"],
        "weight": [1.0, 4.0],
        "eff_date": [date(2024, 6, 1)] * 2,
    }))
    assert repo.count() == 2
    as_of = repo.as_of("000300.SH", date(2024, 7, 1))
    assert as_of["symbol"].to_list() == ["A", "D"]
    assert repo.symbols_as_of("000300.SH", date(2024, 7, 1)) == ["A", "D"]
    assert repo.as_of("000300.SH", date(2024, 1, 1)).height == 0  # 快照前 → 空帧
    assert repo.latest_symbols("000300.SH") == ["A", "D"]
    assert repo.latest_symbols("NOPE.SH") == []


def test_index_cons_multi_batch(db):
    from lquant.data.store.catalog import IndexConsRepo

    repo = IndexConsRepo()
    repo.upsert(pl.DataFrame({
        "index_code": ["000300.SH", "000905.SH"],
        "symbol": ["A", "B"],
        "eff_date": [date(2024, 6, 1), date(2024, 6, 1)],
    }))
    assert repo.symbols_as_of("000905.SH", date(2024, 7, 1)) == ["B"]
    assert repo.as_of("000300.SH", date(2024, 7, 1))["symbol"].to_list() == ["A"]


def test_index_cons_upsert_filters_epoch_keys(db):
    """epoch_cols 里的键不在 df 列中 → 静默跳过该键（不炸）。"""
    from lquant.data.store.catalog import upsert

    assert upsert("index_cons", pl.DataFrame({"symbol": ["A"]})) == 1
