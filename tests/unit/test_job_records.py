"""job_record 持久化：enqueue 落库 / 终态回写 / 启动标记 interrupted / WS 兜底。"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """隔离 LQ_ROOT + 强制本地降级模式（无 Redis）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lquant.server.jobs._redis_available", lambda ttl=0: False)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _rec(job_id, expect: str | None = None):
    """轮询取记录（flusher 异步落库），expect 给定时等到该状态。"""
    import time as _t

    from lquant.server import jobs as _j
    from lquant.server.jobs import get_job_record

    deadline = _t.monotonic() + 3
    while True:
        rec = get_job_record(job_id)
        if rec is not None and (expect is None or rec["status"] == expect):
            return rec
        if _t.monotonic() >= deadline:
            # 诊断：队列是否排空 / flusher 是否存活 / 直读 DB
            from lquant.core.db import writer as _w

            with _w() as con:
                try:
                    rows = con.execute(
                        "SELECT job_id, status FROM job_record").fetchall()
                except Exception as e:  # noqa: BLE001
                    rows = f"read err: {e}"
            print(f"\n[diag] rec={rec} queue_empty={_j._RECORD_QUEUE.empty()} "
                  f"alive={_j._RECORD_THREAD.is_alive() if _j._RECORD_THREAD else None} "
                  f"rows={rows}")
            return rec
        _t.sleep(0.05)


def _rec_none(job_id) -> None:
    """flusher 异步落库：确认「无记录」前先等队列排空。"""

    from lquant.server.jobs import _RECORD_QUEUE, get_job_record

    _RECORD_QUEUE.join()
    assert get_job_record(job_id) is None


def test_enqueue_persists_record_started_then_finished(local_env) -> None:
    from lquant.server.jobs import enqueue

    job = enqueue("lquant-mining", lambda: "ok", name="因子评价")
    rec = _rec(job.id, expect="finished")  # 异步落库 + 任务瞬时完成
    assert rec is not None and rec["status"] == "finished"
    assert rec["name"] == "因子评价" and rec["queue"] == "lquant-mining"


def test_enqueue_failure_recorded_with_error(local_env) -> None:
    from lquant.server.jobs import enqueue

    def boom():
        raise ValueError("炸了")

    job = enqueue("lquant-mining", boom)
    job._thread.join(5)
    rec = _rec(job.id, expect="failed")  # 等 flusher 落终态，避免读到 started
    assert rec["status"] == "failed"
    assert "炸了" in rec["error"]


def test_mark_interrupted_on_startup(local_env) -> None:
    from lquant.server.jobs import enqueue, mark_interrupted_jobs

    job = enqueue("lquant-mining", lambda: time.sleep(0.3))
    rec = _rec(job.id)
    assert rec["status"] == "started"
    n = mark_interrupted_jobs()
    assert n >= 1
    assert _rec(job.id)["status"] == "interrupted"
    # 终态不被覆盖
    job._thread.join(5)
    assert _rec(job.id)["status"] in ("finished", "canceled", "failed", "interrupted")


def test_list_recent_merges_db_records(local_env) -> None:
    from lquant.server.jobs import enqueue, list_recent_jobs

    job = enqueue("lquant-mining", lambda: 1, name="因子评价")
    job._thread.join(5)
    _rec(job.id, expect="finished")  # 等 DB 记录落库，内存条目才有 name 可补
    jobs = list_recent_jobs(limit=50)
    ids = [j["id"] for j in jobs]
    assert job.id in ids
    row = next(j for j in jobs if j["id"] == job.id)
    assert row["name"] == "因子评价"


def test_get_job_record_missing(local_env) -> None:
    _rec_none("nope-nope")


def test_ws_fallback_interrupted_record(local_env, monkeypatch) -> None:
    """WS 兜底链：重启后内存注册表失忆 → job_record interrupted → 终态帧。"""
    from fastapi.testclient import TestClient

    from lquant.server import jobs as jobs_mod
    from lquant.server.jobs import enqueue, mark_interrupted_jobs
    from lquant.server.main import create_app

    job = enqueue("lquant-mining", lambda: time.sleep(30))
    from lquant.server.jobs import _RECORD_QUEUE as _rq

    _rq.join()  # 等 INSERT 落库，否则 mark_interrupted 是空更新
    mark_interrupted_jobs()
    # 模拟进程重启：清空进程内注册表（DB 记录仍在）
    monkeypatch.setattr(jobs_mod, "_LOCAL_JOBS", {})
    with TestClient(create_app()) as client, \
            client.websocket_connect(f"/ws/jobs/{job.id}") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "interrupted"
    assert "已中断" in (msg["error"] or "")
    assert msg["done"] is True
    job._canceled = True  # 停掉后台线程，避免测试残留


def test_ws_fallback_finished_record(local_env, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from lquant.server import jobs as jobs_mod
    from lquant.server.jobs import enqueue
    from lquant.server.main import create_app

    job = enqueue("lquant-mining", lambda: "ok")
    job._thread.join(5)
    monkeypatch.setattr(jobs_mod, "_LOCAL_JOBS", {})
    with TestClient(create_app()) as client, \
            client.websocket_connect(f"/ws/jobs/{job.id}") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "finished" and msg["done"] is True
