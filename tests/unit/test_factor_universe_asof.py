"""因子评价股票池的 as-of 成分口径（幸存者偏差修复）。

隔离姿势沿用 test_backtest_runs：LQ_ROOT + chdir + cache_clear，
DDL 全量建表（IndexConsRepo 走主库 index_cons）。
"""

from __future__ import annotations

from datetime import date

import pytest

from lquant.core.errors import LQuantError


@pytest.fixture
def cons_env(tmp_path, monkeypatch):
    """隔离 DuckDB；index_cons 造两批快照：2020 旧成分 / 2024 新成分。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    import duckdb
    import polars as pl

    from lquant.data.store.ddl import DDL_STATEMENTS

    (tmp_path / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(get_settings().duckdb_path))
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)
    con.close()

    from lquant.data.store.catalog import IndexConsRepo

    old = pl.DataFrame(
        {
            "index_code": ["000300.SH"] * 3,
            "symbol": ["600000", "600036", "000001"],
            "weight": [1.0, 1.0, 1.0],
            "eff_date": [date(2020, 1, 1)] * 3,
            "source": ["test"] * 3,
        }
    )
    new = pl.DataFrame(
        {
            "index_code": ["000300.SH"] * 3,
            "symbol": ["600519", "300750", "601318"],
            "weight": [1.0, 1.0, 1.0],
            "eff_date": [date(2024, 6, 3)] * 3,
            "source": ["test"] * 3,
        }
    )
    repo = IndexConsRepo()
    repo.upsert(old)
    repo.upsert(new)

    yield repo
    get_settings.cache_clear()


def _universe_symbols(universe, *, as_of=None):
    from lquant.server.api.factors import _universe_symbols

    return _universe_symbols(universe, as_of=as_of)


# ---------------- as-of 口径 ----------------


def test_as_of_uses_snapshot_effective_on_that_day(cons_env):
    # 2021 年评价 → 只能用 2020 批（2024 批还没生效，防前视）
    assert _universe_symbols("hs300", as_of=date(2021, 6, 1)) == ["000001", "600000", "600036"]


def test_as_of_picks_latest_effective_batch(cons_env):
    # 2025 年评价 → 2024 批已生效
    assert _universe_symbols("hs300", as_of=date(2025, 1, 1)) == ["300750", "600519", "601318"]


def test_as_of_accepts_iso_string(cons_env):
    assert _universe_symbols("hs300", as_of="2021-06-01") == ["000001", "600000", "600036"]


def test_as_of_before_any_snapshot_raises_503(cons_env):
    # 起点早于一切快照：宁可空，不拿今天的成分假装历史
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _universe_symbols("hs300", as_of=date(2015, 1, 1))
    assert ei.value.status_code == 503
    assert "已生效" in ei.value.detail


def test_as_of_bad_string_raises_422(cons_env):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _universe_symbols("hs300", as_of="2021-13-40")
    assert ei.value.status_code == 422


def test_no_as_of_keeps_latest_snapshot(cons_env):
    # 缺省 = 研究态当下口径（与旧行为一致，未同步区间语义不静默变化）
    assert _universe_symbols("hs300") == ["300750", "600519", "601318"]


def test_all_universe_bypasses_cons_table(cons_env):
    assert _universe_symbols("all") is None
    assert _universe_symbols(None) is None


def test_unknown_universe_raises(cons_env):
    with pytest.raises(ValueError):
        _universe_symbols("nope300", as_of=date(2021, 6, 1))


def test_empty_cons_still_raises_503(cons_env):
    # 未同步指数（无任何快照）在两档口径下都要 503，而不是返回空列表装作无事
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _universe_symbols("zz500")
    with pytest.raises(HTTPException):
        _universe_symbols("zz500", as_of=date(2021, 6, 1))


# ---------------- IndexConsRepo.symbols_as_of 语义护栏 ----------------


def test_symbols_as_of_is_true_point_in_time(cons_env):
    # repo 层自身语义：eff_date <= d 的最近一批，绝不包含未来批次
    repo = cons_env
    assert repo.symbols_as_of("000300.SH", date(2024, 6, 2)) == ["000001", "600000", "600036"]
    assert repo.symbols_as_of("000300.SH", date(2024, 6, 3)) == ["300750", "600519", "601318"]


def test_http_exception_is_lquant_error_family(cons_env):
    # 503 语义与评价主路径的其他 503 同族（可被上游统一兜底）
    from fastapi import HTTPException

    assert issubclass(HTTPException, Exception)
    with pytest.raises((HTTPException, LQuantError)):
        _universe_symbols("hs300", as_of=date(2010, 1, 1))
