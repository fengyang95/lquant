"""market/backfill 资金流回填与 demo 清理的单元测试（不联网）。

这一组守的是「个股分析的资金面为什么恒为空」的根因：
money_flow 表里只有净流入榜前列的少数标的，且混着合成数据。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import polars as pl
import pytest

import lquant.market.backfill as bf
import lquant.market.collectors.money_flow as mf
from lquant.core.config import get_settings


@pytest.fixture()
def flow_env(tmp_path, monkeypatch):
    """隔离 LQ_ROOT / CWD，并建好看板表结构。

    注意：``LQ_DUCKDB_PATH`` **不被 get_settings() 读取**（只有 LQ_ROOT 等
    少数变量生效），真正的隔离来自 ``chdir(tmp_path)`` —— ``duckdb_path``
    默认是相对 CWD 的 './data/duckdb/lquant.duckdb'。
    purge_demo_flow 会 DELETE，所以这里显式断言不会落到仓库真实湖。
    """
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    from pathlib import Path

    from lquant.core.db import writer
    from lquant.market.schema import ensure_market_tables

    assert Path(get_settings().duckdb_path).resolve().is_relative_to(tmp_path), (
        "测试库没被隔离到 tmp_path —— purge 会删真实湖的数据")
    with writer() as con:
        ensure_market_tables(con)
    yield tmp_path
    get_settings.cache_clear()


def _scalar(sql: str) -> int:
    from lquant.core.db import reader

    with reader() as con:
        return con.execute(sql).fetchone()[0]


def _insert_flow(rows: list[tuple]) -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.executemany(
            "INSERT INTO money_flow (trade_date, symbol, name, main_net_inflow, source) "
            "VALUES (?,?,?,?,?)", rows)


def _flow_df(sym: str, n: int = 5) -> pl.DataFrame:
    base = date(2026, 9, 1)
    rows = [{"trade_date": base + timedelta(days=i), "symbol": sym, "name": "某股",
             "close": 10.0, "change_pct": 1.0, "main_net_inflow": 1e7,
             "main_net_ratio": 5.0, "super_large_net": 6e6, "large_net": 4e6,
             "medium_net": -3e6, "small_net": -2e6, "source": "history",
             "collected_at": datetime(2026, 9, 30)} for i in range(n)]
    return pl.DataFrame(rows, schema=mf._flow_schema())


# ------------------------------------------------------------ 真实行判定


def test_real_flow_predicate_full_schema():
    con = duckdb.connect()
    con.execute("CREATE TABLE money_flow (trade_date DATE, symbol VARCHAR, "
                "name VARCHAR, source VARCHAR)")
    pred = bf.real_flow_predicate(con)
    assert "source" in pred and "name" in pred and "样例" in pred


def test_real_flow_predicate_without_source_column():
    """老库没有 source 列时不能引用它（否则整个读取直接报错）。"""
    con = duckdb.connect()
    con.execute("CREATE TABLE money_flow (trade_date DATE, symbol VARCHAR, name VARCHAR)")
    pred = bf.real_flow_predicate(con)
    assert "source" not in pred
    assert "样例" in pred


def test_real_flow_predicate_without_name_column():
    con = duckdb.connect()
    con.execute("CREATE TABLE money_flow (trade_date DATE, symbol VARCHAR, source VARCHAR)")
    pred = bf.real_flow_predicate(con)
    assert "name" not in pred and "source" in pred


def test_real_flow_predicate_unknown_table_is_true():
    """表都不存在时返回 TRUE：无从判断时宁可全当真，不静默吞数据。"""
    assert bf.real_flow_predicate(duckdb.connect()) == "TRUE"


# ------------------------------------------------------------ 合成数据清理


def test_purge_demo_flow_removes_both_generations(flow_env):
    """两代 demo 行都要清：带 source 的、以及只靠名字能认出来的老行。"""
    _insert_flow([
        (date(2026, 9, 1), "600519.SH", "贵州茅台", 1e7, None),      # 老真实行
        (date(2026, 9, 1), "600013.SH", "样例001", 5e7, None),       # 老 demo 行
        (date(2026, 10, 2), "300408.SZ", "样例088", 9e7, None),      # 老 demo 撞真实标的
        (date(2026, 10, 6), "000001.SZ", "平安银行", 2e7, "demo"),   # 带 source 的 demo
        (date(2026, 10, 6), "600036.SH", "招商银行", 3e7, "eastmoney"),
    ])
    preview = bf.purge_demo_flow(dry_run=True)
    assert preview == {"dry_run": True, "demo_rows": 3, "relabel_rows": 1,
                       "deleted": 0}
    assert _scalar("SELECT count(*) FROM money_flow") == 5      # dry_run 不删

    rep = bf.purge_demo_flow()
    assert rep["deleted"] == 3 and rep["relabel_rows"] == 1
    assert _scalar("SELECT count(*) FROM money_flow") == 2
    assert _scalar("SELECT count(*) FROM money_flow WHERE source IS NULL") == 0
    assert _scalar("SELECT count(*) FROM money_flow WHERE source = 'eastmoney'") == 2
    assert bf._flow_have() == {"600519.SH": 1, "600036.SH": 1}


def test_purge_demo_flow_noop_on_clean_table(flow_env):
    _insert_flow([(date(2026, 9, 1), "600519.SH", "贵州茅台", 1e7, "eastmoney")])
    rep = bf.purge_demo_flow()
    assert rep["deleted"] == 0 and rep["relabel_rows"] == 0
    assert _scalar("SELECT count(*) FROM money_flow") == 1


# ------------------------------------------------------------ 历史回填


def test_backfill_persists_and_reports_coverage(flow_env, monkeypatch):
    calls: list[str] = []

    def fake(sym, **kw):
        calls.append(sym)
        return _flow_df(sym, 5)

    monkeypatch.setattr(mf, "fetch_money_flow_history", fake)
    rep = bf.backfill_money_flow_history(["600519", "000001.SZ"])

    assert calls == ["600519.SH", "000001.SZ"]      # 裸码归一 + 保序
    assert rep["fetched"] == 10 and rep["persisted"] == 10
    assert rep["covered_before"] == 0 and rep["covered_after"] == 2
    assert rep["failed"] == {} and rep["symbols"] == 2
    assert _scalar("SELECT count(*) FROM money_flow") == 10
    assert _scalar("SELECT count(DISTINCT symbol) FROM money_flow") == 2


def test_backfill_dedupes_symbol_forms(flow_env, monkeypatch):
    """600519 与 600519.SH 是同一只，不该拉两次。"""
    calls: list[str] = []
    monkeypatch.setattr(mf, "fetch_money_flow_history",
                        lambda sym, **kw: (calls.append(sym), _flow_df(sym, 1))[1])
    rep = bf.backfill_money_flow_history(["600519", "600519.SH", "600519"])
    assert calls == ["600519.SH"]
    assert rep["symbols"] == 1


def test_backfill_single_failure_does_not_abort_batch(flow_env, monkeypatch):
    def fake(sym, **kw):
        if sym == "600519.SH":
            raise RuntimeError("单票接口 500")
        return _flow_df(sym, 3)

    monkeypatch.setattr(mf, "fetch_money_flow_history", fake)
    rep = bf.backfill_money_flow_history(["600519.SH", "000001.SZ"])
    assert "单票接口 500" in rep["failed"]["600519.SH"]
    assert rep["covered_after"] == 1
    assert _scalar("SELECT count(*) FROM money_flow") == 3


def test_backfill_batches_writes(flow_env, monkeypatch):
    """分批落库：攒够 _FLOW_BATCH 只就写一次，报告里累计。"""
    monkeypatch.setattr(bf, "_FLOW_BATCH", 2)
    monkeypatch.setattr(mf, "fetch_money_flow_history",
                        lambda sym, **kw: _flow_df(sym, 2))
    rep = bf.backfill_money_flow_history(
        ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"])
    assert rep["fetched"] == 10 and rep["persisted"] == 10
    assert _scalar("SELECT count(*) FROM money_flow") == 10


def test_backfill_passes_days_through(flow_env, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        mf, "fetch_money_flow_history",
        lambda sym, **kw: (seen.append((sym, kw)), _flow_df(sym, 1))[1])
    bf.backfill_money_flow_history(["600519.SH"], days=30, demo=True, qps=0.5)
    assert seen == [("600519.SH", {"days": 30, "demo": True, "qps": 0.5})]


def test_backfill_empty_symbol_list(flow_env):
    rep = bf.backfill_money_flow_history([])
    assert rep["symbols"] == 0 and rep["fetched"] == 0 and rep["persisted"] == 0


# ------------------------------------------------------------ 标的池


def test_money_flow_symbols_empty_lake(flow_env):
    assert bf.money_flow_symbols() == []


def test_money_flow_symbols_reads_daily_lake(flow_env):
    out = flow_env / "data" / "parquet" / "daily" / "year=2026"
    out.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600519.SH", "000001.SZ", "600519.SH"],
                  "close": [1.0, 2.0, 3.0]}).write_parquet(out / "part-0.parquet")
    assert bf.money_flow_symbols() == ["000001.SZ", "600519.SH"]
    assert bf.money_flow_symbols(limit=1) == ["000001.SZ"]


def test_flow_have_missing_table_returns_empty(flow_env):
    """表没建时覆盖统计要降级为空，而不是把 status 命令带崩。"""
    from lquant.core.db import writer

    with writer() as con:
        con.execute("DROP TABLE money_flow")
    assert bf._flow_have() == {}


def test_dedupe_symbols_skips_blanks_keeps_unparsable():
    assert bf._dedupe_symbols(["", "   ", "600519", "abc", "600519.SH", "abc"]) == \
        ["600519.SH", "abc"]


def test_real_flow_predicate_survives_name_only_schema():
    """只有 name、没有 source 的老库：谓词仍要能用（demo 行靠名字认）。"""
    con = duckdb.connect()
    con.execute("CREATE TABLE money_flow (trade_date DATE, symbol VARCHAR, name VARCHAR)")
    con.execute("INSERT INTO money_flow VALUES (DATE '2026-01-05','600519.SH','贵州茅台')")
    con.execute("INSERT INTO money_flow VALUES (DATE '2026-01-05','600013.SH','样例001')")
    pred = bf.real_flow_predicate(con)
    kept = con.execute(f"SELECT symbol FROM money_flow WHERE {pred}").fetchall()
    assert [r[0] for r in kept] == ["600519.SH"]
