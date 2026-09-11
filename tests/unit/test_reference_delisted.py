"""reference.sync_delisted 退市股名单同步单元测试（mock akshare，不联网）。

隔离方式沿用 test_data_tasks：LQ_ROOT env + chdir + cache_clear。
"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.data.ingest import reference as reference_mod


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


class _FakeAk:
    """akshare 沪深退市接口替身：列名与真实返回一致。"""

    @staticmethod
    def stock_info_sh_delist(symbol: str = "全部") -> pl.DataFrame:
        return pl.DataFrame({
            "公司代码": ["600001", "600002"],
            "公司简称": ["邯郸钢铁", "齐鲁退市"],
            "上市日期": ["1998-01-22", "1998-04-08"],
            "暂停上市日期": ["2009-12-29", "2006-04-24"],
        })

    @staticmethod
    def stock_info_sz_delist(symbol: str = "终止上市公司") -> pl.DataFrame:
        return pl.DataFrame({
            "证券代码": ["000003", "000004"],
            "证券简称": ["PT金田Ａ", "国华退"],
            "上市日期": ["1991-01-14", "1990-12-01"],
            "终止上市日期": ["2002-06-14", "2026-07-14"],
        })


@pytest.fixture
def fake_ak(monkeypatch):
    monkeypatch.setattr(reference_mod, "_ak_delist_module", lambda: _FakeAk)


def test_sync_delisted_upserts_all(fake_settings, fake_ak):
    _seed_security([])
    n = reference_mod.sync_delisted()
    assert n == 4
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute(
            "SELECT symbol, name, list_date, delist_date, sec_type "
            "FROM security ORDER BY symbol"
        ).fetchall()
    by_sym = {r[0]: r for r in rows}
    assert by_sym["600001.SH"][1] == "邯郸钢铁"
    assert str(by_sym["600001.SH"][3]) == "2009-12-29"
    assert str(by_sym["000003.SZ"][3]) == "2002-06-14"
    assert by_sym["600001.SH"][4] == "stock"


def test_sync_delisted_preserves_existing_details(fake_settings, fake_ak):
    """库里已有的 list_date/delist_date 不被整行覆盖冲掉。"""
    from datetime import date

    _seed_security([("600001.SH", "stock", date(1998, 1, 22), date(2009, 1, 15))])
    reference_mod.sync_delisted()
    from lquant.core.db import reader

    with reader() as con:
        row = con.execute(
            "SELECT name, list_date, delist_date FROM security WHERE symbol='600001.SH'"
        ).fetchone()
    assert row[0] == "邯郸钢铁"
    # 库里已有 delist_date → 保留旧值（不被官方名单覆盖成另一口径）
    assert str(row[2]) == "2009-01-15"


def test_sync_delisted_empty_source(fake_settings, monkeypatch):
    class _Empty:
        @staticmethod
        def stock_info_sh_delist(symbol: str = "全部") -> pl.DataFrame:
            return pl.DataFrame()

        @staticmethod
        def stock_info_sz_delist(symbol: str = "终止上市公司") -> pl.DataFrame:
            return pl.DataFrame()

    monkeypatch.setattr(reference_mod, "_ak_delist_module", lambda: _Empty)
    _seed_security([])
    assert reference_mod.sync_delisted() == 0


def test_sync_reference_includes_delisted(fake_settings, fake_ak, monkeypatch):
    """sync_reference 串入退市名单，且在 details 之前（慢路径要能遍历到退市股）。"""
    order: list[str] = []
    monkeypatch.setattr(reference_mod, "sync_calendar", lambda *a, **k: order.append("cal") or 1)
    monkeypatch.setattr(reference_mod, "sync_securities", lambda *a, **k: order.append("sec") or 1)
    monkeypatch.setattr(reference_mod, "sync_delisted", lambda *a, **k: order.append("delist") or 1)
    monkeypatch.setattr(
        reference_mod, "sync_security_details",
        lambda *a, **k: order.append("details") or 0)
    out = reference_mod.sync_reference()
    assert order == ["cal", "sec", "delist", "details"]
    assert out["delisted"] == 1
