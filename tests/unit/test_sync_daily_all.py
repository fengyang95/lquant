"""sync daily 作业接数据任务执行器（T7）。

market:"all"（默认）→ create_task("daily_update") + execute_task；
market:"sentinel" 走旧 backfill_daily 哨兵池路径；create_task 冲突 → failed 不炸。
隔离方式沿用 task-3-report 经验：LQ_ROOT 拷库 + chdir + get_settings.cache_clear()，
不 patch get_settings 模块属性。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from lquant.core.config import get_settings
from lquant.core.db import reader, writer
from lquant.data.ingest import daily as daily_mod
from lquant.data.ingest import tasks as data_tasks
from lquant.data.ingest.tasks import TaskConflictError
from lquant.sync import manager

LQ_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("LQ_SYNC_WORKER", "0")

_DB_OK = (LQ_ROOT / "data" / "duckdb" / "lquant.duckdb").exists()


@pytest.fixture(scope="module")
def sync_env(tmp_path_factory):
    """模块级隔离数据环境（拷真实 duckdb + parquet 湖）；teardown 恢复 cwd。"""
    if not _DB_OK:
        pytest.skip("需要本地 data/duckdb/lquant.duckdb（不入库，CI 上跳过）",
                    allow_module_level=True)
    base = tmp_path_factory.mktemp("sync_daily_all")
    orig_cwd = os.getcwd()
    os.chdir(base)
    try:
        (base / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
        shutil.copy2(LQ_ROOT / "data" / "duckdb" / "lquant.duckdb",
                     base / "data" / "duckdb" / "lquant.duckdb")
        src_parquet = LQ_ROOT / "data" / "parquet"
        dst_parquet = base / "data" / "parquet"
        if src_parquet.is_dir():
            shutil.copytree(src_parquet, dst_parquet, dirs_exist_ok=True)
        else:
            dst_parquet.mkdir(parents=True, exist_ok=True)
        get_settings.cache_clear()
        yield base
        get_settings.cache_clear()
    finally:
        os.chdir(orig_cwd)


def _seed_security() -> None:
    """隔离库无 security 表 —— 建表并插 2 只在市股 + 1 只 ETF。"""
    with writer() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS security (
                symbol VARCHAR PRIMARY KEY, name VARCHAR, sec_type VARCHAR,
                board VARCHAR, list_date DATE, delist_date DATE,
                is_st BOOLEAN, source VARCHAR, updated_at TIMESTAMP)
        """)
        con.execute("DELETE FROM security")
        con.executemany(
            "INSERT INTO security VALUES (?, ?, ?, NULL, NULL, NULL, FALSE, 'test', now())",
            [("000001.SZ", "平安银行", "stock"), ("600519.SH", "贵州茅台", "stock"),
             ("510300.SH", "沪深300ETF", "etf")])


def _job(params: dict) -> dict:
    return {"sync_id": "daily", "name": "日线增量同步", "kind": "daily",
            "params": params}


def _ensure_tables(con) -> None:
    manager._ensure_tables(con)


def _sync_runs() -> list[dict]:
    with reader() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT kind, status, detail FROM sync_run ORDER BY started_at"
        ).fetchall()
    return [{"kind": r[0], "status": r[1],
             "detail": json.loads(r[2]) if r[2] else {}} for r in rows]


def test_seed_default_daily_params_has_market_all():
    """纯常量断言，不需要隔离数据环境。"""
    job = next(j for j in manager.DEFAULT_JOBS if j["sync_id"] == "daily")
    assert job["params"] == {"days": 10, "market": "all"}


@pytest.mark.usefixtures("sync_env")
def test_sync_daily_market_all_creates_data_task(monkeypatch):
    called = {}

    def fake_pool(pool, start, end=None, on_progress=None, **kw):
        called["n"] = len(pool)
        if on_progress:
            on_progress({"done": len(pool), "total": len(pool),
                         "failed": [], "rows": 42, "early_stopped": False})
        return {"done": len(pool), "failed": [], "rows": 42,
                "early_stopped": False}

    monkeypatch.setattr(data_tasks, "backfill_pool", fake_pool)
    _seed_security()

    res = manager.run_job(_job({"days": 3, "market": "all"}))

    assert res["status"] == "ok", res["detail"]
    assert res["rows"] == 84   # stocks/etf 两 phase，fake 各回 42
    assert res["detail"]["task_id"]
    assert called["n"] > 0
    # data_task 表多一行 daily_update，终态 ok
    task = data_tasks.get_task(res["detail"]["task_id"])
    assert task is not None and task["kind"] == "daily_update"
    assert task["status"] == "ok" and task["rows_written"] == 84
    # sync_run 有记录且 detail 带 task_id
    runs = [r for r in _sync_runs() if r["kind"] == "daily"]
    assert runs and runs[-1]["status"] == "ok"
    assert runs[-1]["detail"].get("task_id") == res["detail"]["task_id"]


@pytest.mark.usefixtures("sync_env")
def test_sync_daily_market_sentinel_keeps_old_path(monkeypatch):
    called = {}
    monkeypatch.setattr(
        daily_mod, "backfill_daily",
        lambda *, full, start, **kw: called.setdefault("full", full) or 7)
    n_before = len(data_tasks.list_tasks())

    res = manager.run_job(_job({"days": 5, "market": "sentinel"}))

    assert res["status"] == "ok"
    assert res["rows"] == 7
    assert called["full"] is False
    assert len(data_tasks.list_tasks()) == n_before   # 没建 data_task


@pytest.mark.usefixtures("sync_env")
def test_sync_daily_conflict_marks_failed(monkeypatch):
    def boom(kind, params=None):
        raise TaskConflictError("已有运行中的数据任务")

    monkeypatch.setattr("lquant.data.ingest.tasks.create_task", boom)

    res = manager.run_job(_job({"days": 3, "market": "all"}))   # 不应抛出

    assert res["status"] == "failed"
    assert "已有运行中的数据任务" in json.dumps(res["detail"], ensure_ascii=False)
    runs = [r for r in _sync_runs() if r["kind"] == "daily"]
    assert runs and runs[-1]["status"] == "failed"


@pytest.mark.usefixtures("sync_env")
def test_sync_daily_invalid_market_rejected(monkeypatch):
    # run_job 层白名单：非法 market → failed，不抛出、不建 data_task
    res = manager.run_job(_job({"days": 3, "market": "hedge"}))
    assert res["status"] == "failed"
    assert "market" in json.dumps(res["detail"], ensure_ascii=False)

    # upsert_job 层：非法 market 直接 ValueError 拒收
    with pytest.raises(ValueError, match="market"):
        manager.upsert_job("daily", "x", "daily", "18:30", params={"market": "hedge"})
