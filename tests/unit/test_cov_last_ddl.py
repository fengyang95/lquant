"""最后覆盖冲刺：ddl.py 迁移与 lake 视图。"""
from __future__ import annotations

import duckdb
import polars as pl

from lquant.data.store import ddl


def _con():
    return duckdb.connect()


def test_ensure_views_with_files(tmp_path):
    root = tmp_path / "lake"
    (root / "daily" / "dt=2026-01-05").mkdir(parents=True)
    (root / "minute" / "dt=2026-01-05").mkdir(parents=True)
    df = pl.DataFrame({"a": [1]})
    df.write_parquet(root / "daily" / "dt=2026-01-05" / "p.parquet")
    df.write_parquet(root / "minute" / "dt=2026-01-05" / "p.parquet")
    con = duckdb.connect()
    n = ddl.ensure_views(con, root)
    assert n == 2


def test_ensure_views_empty_lake_skips(tmp_path):
    con = duckdb.connect()
    assert ddl.ensure_views(con, tmp_path) == 0


def test_ensure_views_default_dir_branch(tmp_path, monkeypatch):
    # parquet_dir=None → settings 兜底（284-286）
    from types import SimpleNamespace

    import lquant.core.config as cfg

    monkeypatch.setattr(cfg, "get_settings",
                        lambda: SimpleNamespace(parquet_dir=str(tmp_path)))
    con = duckdb.connect()
    assert ddl.ensure_views(con, None) == 0


def test_factor_def_migrations(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE factor_def (name VARCHAR PRIMARY KEY, "
                "expr VARCHAR, enabled BOOLEAN, created_at TIMESTAMP)")
    con.execute("INSERT INTO factor_def VALUES ('a','close',TRUE,NULL)")
    n = ddl.ensure_factor_def_columns(con)
    assert n == 4
    n2 = ddl.ensure_factor_def(con)
    assert n2 == 1
    cols = {r[0] for r in con.execute("DESCRIBE factor_def").fetchall()}
    assert {"name", "expression", "source", "source_ref", "factor_id",
            "category"} <= cols
    assert con.execute("SELECT expression FROM factor_def WHERE name='a'") \
        .fetchone()[0] == "close"
    # 幂等
    assert ddl.ensure_factor_def(con) == 0


def test_ensure_classify_snapshots(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE industry_classify (symbol VARCHAR, std VARCHAR, "
                "code VARCHAR, name VARCHAR, std_date DATE, source VARCHAR)")
    assert ddl.ensure_classify_snapshots(con) == 1
    cols = con.execute("DESCRIBE industry_classify").fetchall()
    assert any(r[3] == "PRI" for r in cols)
    assert ddl.ensure_classify_snapshots(con) == 0


def test_ensure_collect_log():
    con = duckdb.connect()
    con.execute("CREATE TABLE collect_log (job VARCHAR, trade_date DATE)")
    assert ddl.ensure_collect_log(con) == 1
    cols = con.execute("DESCRIBE collect_log").fetchall()
    assert any(r[3] == "PRI" for r in cols)  # 迁移后带主键
    assert ddl.ensure_collect_log(con) == 0  # 幂等
