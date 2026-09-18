"""批次一覆盖补充：data/ingest/reference.py 日历/标的/退市/详情编排。"""

from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import polars as pl
import pytest

from lquant.data.ingest import reference as ref


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_security(rows: list[tuple[str, str, object, object]]) -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE security ("
            "symbol VARCHAR PRIMARY KEY, name VARCHAR, sec_type VARCHAR, "
            "board VARCHAR, list_date DATE, delist_date DATE, is_st BOOLEAN, "
            "source VARCHAR, updated_at TIMESTAMP)"
        )
        for sym, st, ld, dd in rows:
            con.execute(
                "INSERT INTO security VALUES (?, NULL, ?, NULL, ?, ?, NULL, NULL, NULL)",
                [sym, st, ld, dd],
            )


def _seed_calendar_table() -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE trade_calendar ("
            "trade_date DATE PRIMARY KEY, is_open BOOLEAN, exchange VARCHAR)"
        )


def _seed_full_ddl() -> None:
    """全部 DDL 建表（detail 老值 join / merge 测试需要 security 表结构完整）。"""
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for ddl in DDL_STATEMENTS:
            con.execute(ddl)


class _Provider:
    name = "baostock"

    def trade_calendar(self, start, end):
        return pl.DataFrame({
            "trade_date": [date(2024, 1, 2), date(2024, 1, 3)],
            "is_open": [True, False],
            "exchange": ["SSE", "SSE"],
        })

    def securities(self):
        return pl.DataFrame({
            "symbol": ["600000.SH", "000001.SZ"],
            "name": ["浦发银行", "平安银行"],
            "sec_type": ["stock", "stock"],
            "list_date": [date(1999, 11, 10), None],
            "delist_date": [None, None],
        })

    def security_details(self, symbols):
        return pl.DataFrame({
            "symbol": symbols,
            "name": [f"n{s}" for s in symbols],
            "list_date": [date(2000, 1, 1)] * len(symbols),
            "delist_date": [None] * len(symbols),
        })


class _Chain:
    def __init__(self, providers) -> None:
        self.providers = providers


def _patch(monkeypatch, provider):
    import lquant.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "get_provider", lambda: provider)


# ---------- sync_calendar / sync_securities ----------


def test_sync_calendar_upserts(fake_settings, monkeypatch):
    _seed_calendar_table()
    _patch(monkeypatch, _Provider())
    n = ref.sync_calendar("2024-01-01", "2024-12-31")
    assert n == 2
    from lquant.core.db import reader

    with reader() as con:
        n_all = con.execute("SELECT count(*) FROM trade_calendar").fetchone()[0]
    assert n_all == 2


def test_sync_calendar_empty(fake_settings, monkeypatch):
    p = _Provider()
    monkeypatch.setattr(p, "trade_calendar", lambda s, e: pl.DataFrame())
    _seed_security([])
    _patch(monkeypatch, p)
    assert ref.sync_calendar() == 0


def test_sync_securities_merges_existing(fake_settings, monkeypatch):
    _seed_security([("600000.SH", "stock", date(1990, 1, 1), None)])
    _patch(monkeypatch, _Provider())
    n = ref.sync_securities()
    assert n == 2
    from lquant.core.db import reader

    with reader() as con:
        ld = con.execute(
            "SELECT list_date FROM security WHERE symbol = '600000.SH'"
        ).fetchone()[0]
    assert str(ld) == "1999-11-10"  # 默认新值优先（keep_existing=False）


def test_sync_securities_empty(fake_settings, monkeypatch):
    p = _Provider()
    monkeypatch.setattr(p, "securities", lambda: pl.DataFrame())
    _seed_security([])
    _patch(monkeypatch, p)
    assert ref.sync_securities() == 0


def test_sync_securities_without_list_date_cols(fake_settings, monkeypatch):
    """快路径只有 code/name/status —— join 后旧日期仍保留。"""
    _seed_security([("600000.SH", "stock", date(1990, 1, 1), None)])
    p = _Provider()
    monkeypatch.setattr(
        p, "securities",
        lambda: pl.DataFrame({"symbol": ["600000.SH"], "name": ["x"],
                              "sec_type": ["stock"]}))
    _patch(monkeypatch, p)
    assert ref.sync_securities() == 1
    from lquant.core.db import reader

    with reader() as con:
        ld = con.execute(
            "SELECT list_date FROM security WHERE symbol = '600000.SH'"
        ).fetchone()[0]
    assert str(ld) == "1990-01-01"


# ---------- sync_delisted / _normalize_delist ----------


def test_sync_delisted_all_fail(fake_settings, monkeypatch):
    _seed_security([])

    class _BadAk:
        @staticmethod
        def stock_info_sh_delist(symbol="全部"):
            raise RuntimeError("net")

        @staticmethod
        def stock_info_sz_delist(symbol="终止上市公司"):
            raise RuntimeError("net")

    monkeypatch.setattr(ref, "_ak_delist_module", lambda: _BadAk)
    assert ref.sync_delisted() == 0


def test_sync_delisted_partial_fail(fake_settings, monkeypatch):
    """一家失败不拖垮另一家。"""
    _seed_security([])

    class _HalfAk:
        @staticmethod
        def stock_info_sh_delist(symbol="全部"):
            raise RuntimeError("net")

        @staticmethod
        def stock_info_sz_delist(symbol="终止上市公司"):
            return pl.DataFrame({
                "证券代码": ["000003"],
                "证券简称": ["PT金田Ａ"],
                "上市日期": ["1991-01-14"],
                "终止上市日期": ["2002-06-14"],
            })

    monkeypatch.setattr(ref, "_ak_delist_module", lambda: _HalfAk)
    assert ref.sync_delisted() == 1


def test_ak_delist_module_real_import(monkeypatch):
    fake = type(sys)("akshare")
    monkeypatch.setitem(sys.modules, "akshare", fake)
    assert ref._ak_delist_module() is fake


def test_normalize_delist_empty_and_b_share():
    assert len(ref._normalize_delist(None, "a", "b", "c", "d")) == 0
    empty = ref._normalize_delist(pd.DataFrame(), "a", "b", "c", "d")
    assert len(empty) == 0
    df = pd.DataFrame({
        "code": ["600001", "200002", "000003"],
        "name": ["A", "B股", "C"],
        "ld": ["1998-01-22", "1999-01-01", "1991-01-14"],
        "dd": ["2009-12-29", "2010-01-01", "2002-06-14"],
    })
    out = ref._normalize_delist(df, "code", "name", "ld", "dd")
    assert out["symbol"].to_list() == ["600001.SH", "000003.SZ"]  # B股丢行
    assert out["sec_type"].unique().to_list() == ["stock"]


def test_normalize_delist_keeps_polars_input():
    df = pl.DataFrame({
        "code": ["600001"], "name": ["A"],
        "ld": ["1998-01-22"], "dd": ["2009-12-29"],
    })
    out = ref._normalize_delist(df, "code", "name", "ld", "dd")
    assert out["symbol"][0] == "600001.SH"


# ---------- sync_security_details ----------


class _FakeRepo:
    def __init__(self, pending: list[str]) -> None:
        self.pending = pending
        self.upserts: list[pl.DataFrame] = []
        SecurityRepo_calls.append(self)

    def pending_details(self, limit=None):
        p = self.pending[:limit] if limit else self.pending
        return list(p)

    def upsert(self, df):
        self.upserts.append(df)
        return len(df)


SecurityRepo_calls: list = []


def test_sync_security_details_full_flow(fake_settings, monkeypatch):
    _seed_security([("600000.SH", "stock", None, None)])
    repo = _FakeRepo([])
    SecurityRepo_calls.clear()
    repo.pending = ["600000.SH", "000001.SZ"]
    monkeypatch.setattr(ref, "SecurityRepo", lambda: _FakeRepo(repo.pending))
    _patch(monkeypatch, _Chain([_Provider()]))
    # 库里已有 board/is_st —— details 不带这两列，写前要 join 回来
    from lquant.core.db import writer

    with writer() as con:
        con.execute("UPDATE security SET board = '主板', is_st = FALSE")
    done = ref.sync_security_details(batch=1)
    assert done == 2
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute(
            "SELECT symbol, board FROM security ORDER BY symbol").fetchall()
    assert dict(rows)["600000.SH"] == "主板"  # 旧 board 没被 OR REPLACE 冲掉


def test_sync_security_details_empty_pending(fake_settings, monkeypatch):
    repo = _FakeRepo([])
    monkeypatch.setattr(ref, "SecurityRepo", lambda: repo)
    assert ref.sync_security_details() == 0


def test_sync_security_details_limit(fake_settings, monkeypatch):
    repo = _FakeRepo(["a", "b", "c"])
    monkeypatch.setattr(ref, "SecurityRepo", lambda: repo)
    _patch(monkeypatch, _Chain([_Provider()]))
    done = ref.sync_security_details(limit=2, batch=2)
    assert done == 2


def test_sync_security_details_checkpoint_skip(fake_settings, monkeypatch):
    """checkpoint 已标记 → todo 空，直接返回 0。"""
    monkeypatch.setattr(ref, "SecurityRepo", lambda: _FakeRepo(["a"]))
    from lquant.data.ingest.checkpoint import Checkpoint

    Checkpoint("security_details").mark(["a"])
    assert ref.sync_security_details() == 0


def test_sync_security_details_reader_error(fake_settings, monkeypatch):
    """读旧 board/is_st 失败 → 按无旧值处理，写入照常。"""
    repo = _FakeRepo(["a"])
    monkeypatch.setattr(ref, "SecurityRepo", lambda: repo)
    _patch(monkeypatch, _Chain([_Provider()]))

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ref, "reader", _boom)
    done = ref.sync_security_details()
    assert done == 1


def test_sync_security_details_solo_provider(fake_settings, monkeypatch):
    """provider 非链（无 .providers）→ 直接用链头。"""
    repo = _FakeRepo(["a"])
    monkeypatch.setattr(ref, "SecurityRepo", lambda: repo)
    _patch(monkeypatch, _Provider())
    done = ref.sync_security_details()
    assert done == 1


# ---------- sync_reference ----------


def test_sync_reference_partial_failure_raises(fake_settings, monkeypatch):
    calls = []

    def ok(name):
        def fn(*_args, **_kwargs):
            calls.append(name)
            return 1

        return fn

    monkeypatch.setattr(ref, "sync_calendar", ok("cal"))
    monkeypatch.setattr(ref, "sync_securities", ok("sec"))
    monkeypatch.setattr(ref, "sync_delisted", ok("del"))
    monkeypatch.setattr(ref, "sync_security_details", ok("det"))
    out = ref.sync_reference()
    assert out == {"calendar": 1, "securities": 1, "delisted": 1, "details": 1}
    assert out == {"calendar": 1, "securities": 1, "delisted": 1, "details": 1}

    def bad():
        raise RuntimeError("x")

    monkeypatch.setattr(ref, "sync_calendar", bad)
    monkeypatch.setattr(ref, "sync_securities", ok("sec"))
    monkeypatch.setattr(ref, "sync_delisted", ok("del"))
    monkeypatch.setattr(ref, "sync_security_details", ok("det"))
    with pytest.raises(RuntimeError, match="reference 同步部分失败"):
        ref.sync_reference()


def test_sync_reference_skip_details(fake_settings, monkeypatch):
    calls = []

    def ok():
        calls.append(1)
        return 3

    monkeypatch.setattr(ref, "sync_calendar", ok)
    monkeypatch.setattr(ref, "sync_securities", ok)
    monkeypatch.setattr(ref, "sync_delisted", ok)
    out = ref.sync_reference(skip_details=True)
    assert out == {"calendar": 3, "securities": 3, "delisted": 3}
    assert len(calls) == 3


def test_merge_existing_details_reader_error(fake_settings, monkeypatch):
    _seed_security([])

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ref, "reader", _boom)
    df = pl.DataFrame({"symbol": ["600000.SH"]})
    assert ref._merge_existing_details(df) is df  # 异常 → 原样返回


def test_merge_existing_details_keep_existing(fake_settings):
    _seed_security([("600000.SH", "stock", date(1990, 1, 1), None)])
    df = pl.DataFrame({
        "symbol": ["600000.SH"],
        "list_date": [date(1999, 11, 10)],
        "delist_date": [date(2020, 1, 1)],
    })
    out = ref._merge_existing_details(df, keep_existing=True)
    assert str(out["list_date"][0]) == "1990-01-01"   # 旧值优先
    assert str(out["delist_date"][0]) == "2020-01-01"  # 旧值空 → 新值补

    out2 = ref._merge_existing_details(df, keep_existing=False)
    assert str(out2["list_date"][0]) == "1999-11-10"  # 默认新值优先


def test_merge_existing_details_old_empty(fake_settings):
    _seed_security([])
    df = pl.DataFrame({"symbol": ["600000.SH"], "list_date": [date(1999, 1, 1)]})
    out = ref._merge_existing_details(df)
    assert str(out["list_date"][0]) == "1999-01-01"
