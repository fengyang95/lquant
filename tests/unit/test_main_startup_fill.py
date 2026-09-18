"""server/main.py 覆盖补齐：startup 钩子各分支与 DuckDB 锁 503 处理。

_startup 直调（TestClient 的 with 只会触发它一次且分支难控）；所有副作用
（迁移、中断标记、后台线程）全部 monkeypatch 打桩，离线且快。
"""
from __future__ import annotations

import os
import threading

import duckdb
import pytest
from fastapi import Request

os.environ.setdefault("LQ_SYNC_WORKER", "0")


@pytest.fixture
def startup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_SYNC_WORKER", "0")
    monkeypatch.setattr("time.sleep", lambda s: None)  # 启动兜底线程不真等
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _call_startup(monkeypatch, *, mark=0, mark_raise=False, migrate_raise=False,
                  data_mark=0, news_mark=0):

    from lquant.server import main

    events: dict = {"threads": []}

    monkeypatch.setattr("lquant._rust.loader.print_status", lambda: None,
                        raising=False)
    if mark_raise:
        monkeypatch.setattr("lquant.server.jobs.mark_interrupted_jobs",
                            lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    else:
        monkeypatch.setattr("lquant.server.jobs.mark_interrupted_jobs", lambda: mark)

    def fake_ensure(name):
        return lambda con: (events.setdefault("migrate", []).append(name), True)[1]

    monkeypatch.setattr("lquant.data.store.ddl.DDL_STATEMENTS", [],
                        raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_factor_def",
                        fake_ensure("factor_def"), raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_collect_log",
                        fake_ensure("collect_log"), raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_classify_snapshots",
                        fake_ensure("classify"), raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_factor_def_columns",
                        fake_ensure("columns"), raising=False)
    if migrate_raise:
        def _boom(con):
            raise RuntimeError("migrate down")

        monkeypatch.setattr("lquant.data.store.ddl.ensure_factor_def", _boom)
    monkeypatch.setattr("lquant.data.ingest.tasks.mark_interrupted_on_startup",
                        lambda: data_mark, raising=False)
    monkeypatch.setattr("lquant.news.tasks.init_news_task_ddl",
                        lambda con: None, raising=False)
    monkeypatch.setattr("lquant.news.tasks.mark_interrupted_on_startup",
                        lambda con=0: news_mark, raising=False)
    monkeypatch.setattr("lquant.sync.manager.seed_defaults", lambda: 0,
                        raising=False)
    monkeypatch.setattr("lquant.sync.manager.loop_forever",
                        lambda interval: events["threads"].append("sync"),
                        raising=False)
    monkeypatch.setattr("lquant.market.backfill.ensure_market_coverage",
                        lambda days: {"missing_before": {"index_daily": 3},
                                      "persisted": 5}, raising=False)

    if not migrate_raise:
        main._startup()
    else:
        main._startup()
    return events


def test_startup_happy_path(startup_env, monkeypatch) -> None:
    _call_startup(monkeypatch, mark=2, data_mark=3, news_mark=1)
    # 不抛即通过；成功标记数走 print 分支


def test_startup_failure_branches_swallowed(startup_env, monkeypatch) -> None:
    """各启动子步骤失败只 print 跳过，不阻断启动。"""
    _call_startup(monkeypatch, mark_raise=True, migrate_raise=True)


def test_startup_data_news_seed_failure_swallowed(startup_env, monkeypatch) -> None:
    """数据任务/资讯任务/同步种子失败 → print 跳过，不阻断启动。"""
    monkeypatch.setenv("LQ_SYNC_WORKER", "1")
    from lquant.server import main

    monkeypatch.setattr("lquant._rust.loader.print_status", lambda: None,
                        raising=False)
    monkeypatch.setattr("lquant.server.jobs.mark_interrupted_jobs", lambda: 0)
    monkeypatch.setattr("lquant.data.store.ddl.DDL_STATEMENTS", [],
                        raising=False)

    def _boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr("lquant.data.store.ddl.ensure_factor_def", _boom,
                        raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_collect_log", _boom,
                        raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_classify_snapshots", _boom,
                        raising=False)
    monkeypatch.setattr("lquant.data.store.ddl.ensure_factor_def_columns", _boom,
                        raising=False)
    monkeypatch.setattr("lquant.data.ingest.tasks.mark_interrupted_on_startup", _boom,
                        raising=False)
    monkeypatch.setattr("lquant.news.tasks.init_news_task_ddl", _boom, raising=False)
    monkeypatch.setattr("lquant.news.tasks.mark_interrupted_on_startup", _boom,
                        raising=False)
    monkeypatch.setattr("lquant.sync.manager.seed_defaults", _boom, raising=False)
    done = threading.Event()
    monkeypatch.setattr("lquant.sync.manager.loop_forever",
                        lambda interval: done.set(), raising=False)
    bf_done = threading.Event()

    def _bf_ok(days):
        bf_done.set()
        return {"persisted": 0}

    monkeypatch.setattr("lquant.market.backfill.ensure_market_coverage", _bf_ok,
                        raising=False)
    main._startup()  # 所有子步骤失败均被吞掉
    assert done.wait(5) and bf_done.wait(5), "启动线程未收尾"


def test_startup_sync_worker_threads(startup_env, monkeypatch) -> None:
    """LQ_SYNC_WORKER=1 → 起 sync worker 与启动补齐线程。"""
    monkeypatch.setenv("LQ_SYNC_WORKER", "1")
    import time as time_mod

    started = {"sync": False, "backfill": False}

    monkeypatch.setattr("lquant.sync.manager.seed_defaults", lambda: 5)
    monkeypatch.setattr("lquant.sync.manager.loop_forever",
                        lambda interval: started.update(sync=True))
    monkeypatch.setattr("lquant.market.backfill.ensure_market_coverage",
                        lambda days: started.update(backfill=True) or {
                            "missing_before": {"index_daily": 3}, "persisted": 5})
    from lquant.server import main

    main._startup()
    deadline = time_mod.monotonic() + 5
    while not (started["sync"] and started["backfill"]):
        assert time_mod.monotonic() < deadline, "启动线程未执行"
        time_mod.sleep(0.01)


def test_startup_backfill_failure_logged(startup_env, monkeypatch) -> None:
    """启动补齐失败 → 打 warn 不阻断。"""
    monkeypatch.setenv("LQ_SYNC_WORKER", "1")

    def boom(days):
        raise RuntimeError("source down")

    monkeypatch.setattr("lquant.sync.manager.seed_defaults", lambda: 0,
                        raising=False)
    monkeypatch.setattr("lquant.sync.manager.loop_forever", lambda interval: None,
                        raising=False)
    monkeypatch.setattr("lquant.market.backfill.ensure_market_coverage", boom,
                        raising=False)
    from lquant.server import main

    main._startup()  # 后台线程内异常被吞；线程很快跑完（sleep 已被补丁短路）


def test_duckdb_lock_to_503(startup_env, monkeypatch) -> None:
    """DuckDB 写锁冲突 → 503 可重试语义；非锁 IOException 原样 500。"""
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    app = create_app()
    fastapi_app = app.app if hasattr(app, "app") else app

    @fastapi_app.get("/__probe_lock")
    async def _probe(request: Request):
        raise duckdb.IOException("Could not set lock on file")

    @fastapi_app.get("/__probe_other")
    async def _probe2(request: Request):
        raise duckdb.IOException("some other io error")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/__probe_lock")
        assert r.status_code == 503, r.text
        assert "数据湖忙" in r.json()["detail"]
        r2 = client.get("/__probe_other")
        assert r2.status_code == 500
