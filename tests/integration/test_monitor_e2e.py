"""监控全链路集成：http + RQ worker 子进程 → 事件落盘 → API 断言。

test_full_chain 走降级路径（无需 Redis，强制 _redis_available=False）；
test_rq_worker_chain 需要本机 Redis（docker compose up -d redis），不可用则 skip。
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.integration


def _redis_up() -> bool:
    from lquant.server.jobs import _redis_available

    return _redis_available()


_REDIS_MARK = pytest.mark.skipif(not _redis_up(), reason="需要 Redis")


def _enqueue_test_task() -> str:
    from lquant.server.jobs import enqueue

    def _echo():
        return 42

    return enqueue("lquant-default", _echo).id


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "mon.duckdb"))
    monkeypatch.setenv("LQ_SYNC_WORKER", "0")
    monkeypatch.setenv("LQ_REDIS_URL",
                       os.getenv("LQ_REDIS_URL", "redis://localhost:6379/0"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_full_chain(env, monkeypatch):
    """降级模式任务事件 → 内存队列 → flusher → metrics_task → API。

    强制 _redis_available=False：本测试意图是验证降级链路，若 Redis 可用则
    jobs.enqueue 会进 RQ 队列无人消费，断言必失败，与降级前提矛盾。
    """
    from fastapi.testclient import TestClient

    import lquant.server.jobs as jobs_mod
    from lquant.monitor import flusher
    from lquant.server.main import create_app

    monkeypatch.setattr(jobs_mod, "_redis_available", lambda: False)
    monkeypatch.setattr("lquant.monitor.flusher._redis_available",
                        lambda: False)
    _enqueue_test_task()
    time.sleep(1)  # 等本地线程任务跑完发事件
    flusher.flush_once()
    with TestClient(create_app()) as client:
        rows = client.get("/api/monitor/tasks?range=1h").json()["data"]["series"]
        assert sum(r["count"] for r in rows) >= 1
        s = client.get("/api/monitor/summary").json()["data"]
        assert "procs" in s and "queues" in s


@_REDIS_MARK
def test_rq_worker_chain(env):
    """RQ worker（burst 模式）执行任务 → 事件经 Redis → flusher 落盘。"""
    from lquant.monitor import flusher
    from lquant.server.jobs import enqueue, get_redis

    def _demo():
        return 1

    job = enqueue("lquant-default", _demo)
    # burst 模式直接在测试进程内跑一个 MonitoringWorker（不起真实子进程，快且稳）
    from lquant.monitor.worker import MonitoringWorker

    w = MonitoringWorker(["lquant-default"],
                         connection=get_redis(), name=f"test-{uuid.uuid4().hex[:8]}")
    w.work(burst=True)
    deadline = time.time() + 10
    n = 0
    while time.time() < deadline:
        flusher.flush_once()
        import duckdb

        from lquant.core.config import get_settings

        n = duckdb.connect(get_settings().monitor_db_path).execute(
            "SELECT count(*) FROM metrics_task WHERE job_id = ?",
            [job.id]).fetchone()[0]
        if n >= 2:  # started + finished
            break
        time.sleep(0.5)
    assert n >= 2, "任务 started/finished 事件应落盘"
