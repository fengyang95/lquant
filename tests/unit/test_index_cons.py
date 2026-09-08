"""指数成分（index_cons）与申万行业（industry_classify）仓库的防前视语义测试。

用临时 DuckDB（monkeypatch catalog 的 reader/writer），离线可跑。
重点：as_of 严格按生效日过滤，绝不让「今天的成分」污染历史查询。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from lquant.data.store import catalog


@pytest.fixture()
def tmp_catalog(tmp_path: Path, monkeypatch) -> None:
    """把 catalog 的读写连接指到临时库（含全部 core 表）。"""
    db = tmp_path / "lq.duckdb"
    con = duckdb.connect(str(db))
    from lquant.data.store.ddl import DDL_STATEMENTS

    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()

    from contextlib import contextmanager

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


# ---------- index_cons ----------

# 数据模型：一个 eff_date = 一次完整调仓快照（全量成员，含当时被移出的）。
_SNAP1 = date(2026, 3, 2)     # 600000 / 000001 / 600519
_SNAP2 = date(2026, 6, 1)     # +300750（纳入）
_SNAP3 = date(2026, 9, 1)     # -600000 -600519（移出）


def _snap(eff: date, symbols: list[str], base_weight: int = 10) -> pl.DataFrame:
    return pl.DataFrame({
        "index_code": ["000300.SH"] * len(symbols),
        "symbol": symbols,
        "weight": [base_weight - i for i in range(len(symbols))],
        "eff_date": [eff] * len(symbols),
        "source": ["cnindex"] * len(symbols),
    })


def _full_snapshots() -> list[pl.DataFrame]:
    return [_snap(_SNAP1, ["000001.SZ", "600000.SH", "600519.SH"]),
            _snap(_SNAP2, ["000001.SZ", "300750.SZ", "600000.SH", "600519.SH"]),
            _snap(_SNAP3, ["000001.SZ", "300750.SZ"])]


def test_index_cons_as_of_picks_latest_batch(tmp_catalog):
    repo = catalog.IndexConsRepo()
    for s in _full_snapshots():
        repo.upsert(s)
    # 05-15 → 落在 SNAP2 之前 → 取 SNAP1 全量
    assert repo.symbols_as_of("000300.SH", date(2026, 5, 15)) == \
        ["000001.SZ", "600000.SH", "600519.SH"]
    # 06-02 → SNAP2 生效，300750 纳入
    assert repo.symbols_as_of("000300.SH", date(2026, 6, 2)) == \
        ["000001.SZ", "300750.SZ", "600000.SH", "600519.SH"]


def test_index_cons_as_of_lookahead_guarded(tmp_catalog):
    """防前视核心：as_of 当天为界，未来快照的成分不进历史。"""
    repo = catalog.IndexConsRepo()
    for s in _full_snapshots()[:2]:            # 只到 SNAP2
        repo.upsert(s)
    got = repo.symbols_as_of("000300.SH", date(2026, 4, 10))   # SNAP2(06-01)未生效
    assert "300750.SZ" not in got
    assert got == ["000001.SZ", "600000.SH", "600519.SH"]


def test_index_cons_as_of_excludes_removed(tmp_catalog):
    """06-01 调仓后某股从指数移出，后续 as_of 绝不能把它翻出来（幸存者偏差）。"""
    repo = catalog.IndexConsRepo()
    for s in _full_snapshots():
        repo.upsert(s)
    got = repo.symbols_as_of("000300.SH", date(2026, 10, 1))
    assert got == ["000001.SZ", "300750.SZ"]   # 600000/600519 已移出，不在最新快照


def test_index_cons_upsert_replaces_batch_idempotent(tmp_catalog):
    """同一 (index, eff_date) 批次重复 ingest → 整组重写、行数不膨胀。"""
    repo = catalog.IndexConsRepo()
    repo.upsert(_snap(_SNAP2, ["000001.SZ", "300750.SZ", "600000.SH", "600519.SH"]))
    n1 = repo.count()
    # 同批次数据更新（权重/成员变化）重投，必须替换而非追加
    repo.upsert(_snap(_SNAP2, ["000001.SZ", "300750.SZ", "600000.SH", "600519.SH"]))
    assert repo.count() == n1
    # 不同批次共存：SNAP1 + SNAP2 各留一份（历史）
    repo.upsert(_snap(_SNAP1, ["000001.SZ", "600000.SH", "600519.SH"]))
    assert repo.count() == n1 + 3


def test_get_index_stocks_reads_repo(tmp_catalog):
    """JQ shim get_index_stocks 落地到仓库，no-op 不再 NotImplementedError。"""
    repo = catalog.IndexConsRepo()
    for s in _full_snapshots():
        repo.upsert(s)
    from lquant.research.dialect import jq_shim

    # 显式日期：落在 SNAP1 → 看不到 06-01 才纳入的 300750
    got = jq_shim.get_index_stocks("000300.SH", date=date(2026, 5, 15))
    assert got == ["000001.SZ", "600000.SH", "600519.SH"]
    # 默认日期（今天 ≥ 最新快照）不应因参数遮蔽 date 类而崩溃，取最新一批
    latest = jq_shim.get_index_stocks("000300.SH", date=date(2026, 12, 1))
    assert latest == ["000001.SZ", "300750.SZ"]


# ---------- industry_classify ----------

def test_industry_classify_as_of_and_latest(tmp_catalog):
    repo = catalog.IndustryClassifyRepo()
    repo.upsert(pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "std": ["银行", "银行"],
        "code": ["801780", "801780"],
        "std_date": [date(2026, 3, 2), date(2026, 3, 2)],
        "source": ["sw", "sw"]}))
    got = repo.as_of(date(2026, 3, 1))   # 生效日 03-02 未到 → 空
    assert got == {}
    got2 = repo.as_of(date(2026, 3, 3))
    assert got2 == {"600000.SH": "银行", "000001.SZ": "银行"}
    assert repo.latest() == {"600000.SH": "银行", "000001.SZ": "银行"}


def test_industry_classify_change_history(tmp_catalog):
    """行业变迁（std_date）只影响该日之后的中性化，之前仍是旧行业。"""
    repo = catalog.IndustryClassifyRepo()
    repo.upsert(pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "std": ["银行", "多元金融"],
        "code": ["801780", "801790"],
        "std_date": [date(2026, 3, 2), date(2026, 9, 1)],
        "source": ["sw", "sw"]}))
    assert repo.as_of(date(2026, 3, 3)) == {"600000.SH": "银行"}
    assert repo.as_of(date(2026, 9, 2)) == {"600000.SH": "多元金融"}