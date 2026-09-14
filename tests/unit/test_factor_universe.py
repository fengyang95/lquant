"""因子评价的股票池（universe）与时间范围（end）支持。

离线跑：universe 解析走 monkeypatch 的 IndexConsRepo；迁移与模型校验直接断言。
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi import HTTPException

# ---------- ensure_factor_def_columns：category 列 ----------


def test_factor_def_migration_adds_category(tmp_path):
    db = tmp_path / "lq.duckdb"
    con = duckdb.connect(str(db))
    con.execute("""CREATE TABLE factor_def (
        name VARCHAR PRIMARY KEY, expression VARCHAR, description VARCHAR,
        enabled BOOLEAN DEFAULT TRUE, created_at TIMESTAMP,
        source VARCHAR DEFAULT 'manual', source_ref VARCHAR, factor_id VARCHAR
    )""")
    con.close()

    from lquant.data.store.ddl import ensure_factor_def_columns

    con = duckdb.connect(str(db))
    n = ensure_factor_def_columns(con)
    cols = {r[0] for r in con.execute("DESCRIBE factor_def").fetchall()}
    con.close()
    assert "category" in cols
    assert n == 1

    # 幂等：再跑一次不报错、不再加列
    con = duckdb.connect(str(db))
    n2 = ensure_factor_def_columns(con)
    con.close()
    assert n2 == 0


# ---------- universe 解析 ----------


def _fake_repo(symbols_by_index: dict[str, list[str]]):
    class _Fake:
        def __init__(self) -> None:
            pass

        def latest_symbols(self, index_code: str) -> list[str]:
            return symbols_by_index.get(index_code, [])

    return _Fake


def test_resolve_universe_all_returns_none(monkeypatch):
    from lquant.server.api import factors as factors_api

    monkeypatch.setattr(factors_api, "IndexConsRepo", lambda: _fake_repo({})())
    assert factors_api._universe_symbols("all") is None
    assert factors_api._universe_symbols(None) is None


def test_resolve_universe_known_index(monkeypatch):
    from lquant.server.api import factors as factors_api

    repo = _fake_repo(
        {"000300.SH": ["000001.SZ", "600000.SH"], "000906.SH": ["000001.SZ", "600000.SH"]}
    )
    monkeypatch.setattr(factors_api, "IndexConsRepo", lambda: repo())
    assert factors_api._universe_symbols("hs300") == ["000001.SZ", "600000.SH"]
    assert factors_api._universe_symbols("000906.SH") == ["000001.SZ", "600000.SH"]


def test_resolve_universe_empty_raises(monkeypatch):
    from lquant.server.api import factors as factors_api

    repo = _fake_repo({})
    monkeypatch.setattr(factors_api, "IndexConsRepo", lambda: repo())
    with pytest.raises(HTTPException) as ei:
        factors_api._universe_symbols("hs300")
    assert ei.value.status_code == 503


def test_evaluate_in_defaults():
    from lquant.server.api.factors import EvaluateIn

    m = EvaluateIn()
    assert m.universe == "all"
    assert m.end is None
    m2 = EvaluateIn(start="2026-01-01", end="2026-06-30", universe="hs300")
    assert m2.end == "2026-06-30"
    assert m2.universe == "hs300"


def test_evaluate_in_rejects_bad_universe():
    from pydantic import ValidationError

    from lquant.server.api.factors import EvaluateIn

    with pytest.raises(ValidationError):
        EvaluateIn(universe="NOT_AN_INDEX")
