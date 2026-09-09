"""strategy_store：策略/分析库 CRUD + 版本化（tmp DuckDB，自包含）。"""
import pytest

from lquant.backtest.strategy_store import (
    delete_strategy, get_analysis, get_strategy, list_analyses,
    list_strategies, list_versions, save_analysis, save_strategy,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # config.py 的 get_settings 不读 LQ_DUCKDB_PATH，改为隔离 db._path
    monkeypatch.setattr("lquant.core.db._path", lambda: str(tmp_path / "t.duckdb"))
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS
    with writer() as con:
        for s in DDL_STATEMENTS:
            con.execute(s)
    yield


def test_save_and_get(store):
    row = save_strategy("demo", "def initialize(ctx): pass", description="x")
    got = get_strategy(row["id"])
    assert got["source"].startswith("def initialize")
    assert got["version"] == 1


def test_same_name_bumps_version(store):
    a = save_strategy("demo", "def initialize(ctx): pass")
    b = save_strategy("demo", "def initialize(ctx): pass  # v2")
    assert b["version"] == 2
    names = [r["id"] for r in list_strategies() if r["id"] == b["id"]]
    assert names and b["version"] > a["version"]
    assert len(list_versions("demo")) == 2


def test_config_roundtrip(store):
    row = save_strategy("cfg", "def initialize(ctx): pass",
                        config={"cash": 100000, "bench": "000300.SH"})
    got = get_strategy(row["id"])
    assert got["config"] == {"cash": 100000, "bench": "000300.SH"}
    assert got["params"] == {}


def test_soft_delete(store):
    row = save_strategy("tbd", "def initialize(ctx): pass")
    delete_strategy(row["id"])
    assert all(r["id"] != row["id"] for r in list_strategies())
    # 软删：单行读仍可取，版本历史保留并带 deleted 标记
    got = get_strategy(row["id"])
    assert got["source"].startswith("def initialize")
    vers = list_versions("tbd")
    assert len(vers) == 1 and vers[0]["deleted"] is True


def test_analysis_crud(store):
    row = save_analysis("monthly", "def analyze(result): return []")
    assert get_analysis(row["id"])["name"] == "monthly"
    assert any(r["name"] == "monthly" for r in list_analyses())
