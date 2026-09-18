"""最后冲刺第二波：flusher redis/sys 路径 + sync API 错误分支。"""
from __future__ import annotations

import pytest

# ---------- flusher ----------

class _BoomRedis:
    def rpush(self, *a):
        raise RuntimeError("redis down")

    def scan_iter(self, *a, **kw):
        raise RuntimeError("redis down")


def test_requeue_redis_events_failure_swallowed():
    from lquant.monitor import flusher

    flusher._requeue_redis_events(_BoomRedis(), ["a", "b"])  # 不抛


def test_requeue_redis_events_empty_noop():
    from lquant.monitor import flusher

    flusher._requeue_redis_events(None, [])  # 不抛、不触碰 redis


def test_drain_sys_samples_exception_returns_empty():
    from lquant.monitor import flusher

    assert flusher._drain_sys_samples(_BoomRedis()) == []


def test_drain_sys_samples_happy():
    import json as _json

    from lquant.monitor import flusher

    class _R:
        def scan_iter(self, *a, **kw):
            yield b"lquant:monitor:proc:api"

        def get(self, key):
            return _json.dumps({"ts": 1.0, "proc_name": "api",
                                "pid": 7, "cpu_pct": 1.5,
                                "mem_rss_mb": 99.0}).encode()

    out = flusher._drain_sys_samples(_R())
    assert len(out) == 1 and out[0].pid == 7


def test_flusher_redis_indirection():
    from lquant.monitor import flusher

    assert callable(flusher._redis_available)
    assert callable(flusher._get_redis)


# ---------- sync API ----------

@pytest.fixture
def sync_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lquant.server.api import sync as sync_api

    app = FastAPI()
    app.include_router(sync_api.router)
    return TestClient(app), monkeypatch


def test_put_job_422(sync_client):
    c, mp = sync_client

    def boom(*a, **kw):
        raise ValueError("bad kind")

    mp.setattr("lquant.sync.manager.upsert_job", boom)
    r = c.post("/sync/jobs", json={
        "sync_id": "x", "name": "n", "kind": "collect", "schedule_time": "17:30"})
    assert r.status_code == 422


def test_toggle_and_remove(sync_client):
    c, mp = sync_client
    mp.setattr("lquant.sync.manager.list_jobs",
               lambda *a: [{"sync_id": "j1", "enabled": True}])
    mp.setattr("lquant.sync.manager.set_enabled", lambda *a: None)
    mp.setattr("lquant.sync.manager.delete_job", lambda *a: None)
    assert c.post("/sync/jobs/j1/toggle", json={"enabled": False}).status_code == 200
    r404 = c.post("/sync/jobs/nope/toggle", json={"enabled": False})
    assert r404.status_code == 404
    assert c.delete("/sync/jobs/j1").status_code == 200


def test_run_now_adhoc_and_errors(sync_client):
    c, mp = sync_client
    mp.setattr("lquant.sync.manager.list_jobs", lambda *a: [])
    mp.setattr("lquant.sync.manager.run_job", lambda job: {"status": "ok"})
    # 无 sync_id 无 kind → 422
    assert c.post("/sync/run", json={}).status_code == 422
    # sync_id 不存在 → 404
    assert c.post("/sync/run", json={"sync_id": "nope"}).status_code == 404
    # kind 路径（collect + schedule）
    r = c.post("/sync/run", json={"kind": "collect", "demo": False,
                                  "schedule": "17:30"})
    assert r.status_code == 200
    # sync_id 命中 + demo 注入
    mp.setattr("lquant.sync.manager.list_jobs",
               lambda *a: [{"sync_id": "j1", "params": {}}])
    r2 = c.post("/sync/run", json={"sync_id": "j1", "demo": True})
    assert r2.status_code == 200
