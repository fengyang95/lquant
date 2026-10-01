"""monitor.queries 覆盖补齐：Redis/duckdb 打桩下的队列、进程与错误日志查询。"""

from __future__ import annotations

import contextlib
import json
import time
import types

import pytest

from lquant.monitor import queries as q


class _FakeRedis:
    def __init__(self, proc=None, fail=False):
        self._proc = proc or {}
        self._fail = fail
        self.recent = [json.dumps({"event": "finished", "job": "j"})]

    def scan_iter(self, match=None):
        yield from self._proc

    def get(self, key):
        if self._fail:
            raise RuntimeError("redis down")
        return self._proc.get(key)

    def lrange(self, key, a, b):
        if self._fail:
            raise RuntimeError("redis down")
        return self.recent


def _enable_redis(monkeypatch, r):
    from lquant.server import jobs

    monkeypatch.setattr(jobs, "_redis_available", lambda: True)
    monkeypatch.setattr(jobs, "get_redis", lambda: r)


def _disable_redis(monkeypatch):
    from lquant.server import jobs

    monkeypatch.setattr(jobs, "_redis_available", lambda: False)


def test_range_bucket_and_sec_validation() -> None:
    assert q.range_bucket("1h") == 60
    assert q.range_sec("7d") == 7 * 86400
    with pytest.raises(ValueError, match="range 必须是"):
        q.range_bucket("9h")
    with pytest.raises(ValueError, match="range 必须是"):
        q.range_sec("9h")


def test_queue_depths_no_redis(monkeypatch) -> None:
    _disable_redis(monkeypatch)
    out = q.queue_depths()
    assert [r["queue"] for r in out] == [
        "lquant-default", "lquant-ingest", "lquant-backtest", "lquant-mining"]
    assert all(r["pending"] is None for r in out)


def test_queue_depths_redis_error_degrades(monkeypatch) -> None:
    _enable_redis(monkeypatch, _FakeRedis(fail=True))
    out = q.queue_depths()
    assert all(r["pending"] is None for r in out)


def test_queue_depths_with_rq(monkeypatch) -> None:
    r = _FakeRedis()

    class _Registry:
        def __init__(self, name, connection=None):
            self.count = 2

    class _Queue(_Registry):
        count = 3

    fake_rq_queue = types.SimpleNamespace(Queue=_Queue)
    fake_rq_registry = types.SimpleNamespace(
        FailedJobRegistry=_Registry, StartedJobRegistry=_Registry)

    monkeypatch.setitem(__import__("sys").modules, "rq.queue", fake_rq_queue)
    monkeypatch.setitem(__import__("sys").modules, "rq.registry", fake_rq_registry)
    _enable_redis(monkeypatch, r)
    out = q.queue_depths()
    # Queue 实例继承 _Registry.__init__ → self.count=2；pending=2+2
    assert all(r0["pending"] == 4 and r0["failed"] == 2 for r0 in out)


def test_proc_statuses_no_redis_empty(monkeypatch) -> None:
    _disable_redis(monkeypatch)
    assert q.proc_statuses() == []


def test_proc_statuses_online_and_stale(monkeypatch) -> None:
    now = time.time()
    r = _FakeRedis(proc={
        b"lquant:monitor:proc:worker": json.dumps({
            "proc_name": "worker", "pid": 1, "cpu_pct": 5.0,
            "mem_rss_mb": 100.0, "current_job": "sync", "ts": now}),
        b"lquant:monitor:proc:stale": json.dumps({
            "proc_name": "stale", "pid": 2, "ts": now - 999}),
        b"lquant:monitor:proc:empty": None,           # raw 为空 → 跳过
        b"lquant:monitor:proc:noname": json.dumps({
            "pid": 3, "ts": now}),                    # 无 proc_name → 用 key 尾段
    })
    _enable_redis(monkeypatch, r)
    out = q.proc_statuses()
    by = {x["proc_name"]: x for x in out}
    assert by["worker"]["online"] is True
    assert by["stale"]["online"] is False
    assert by["noname"]["proc_name"] == "noname"
    assert "empty" not in by


def test_proc_statuses_error_degrades(monkeypatch) -> None:
    r = _FakeRedis(proc={"k": json.dumps({"ts": time.time()})}, fail=True)
    _enable_redis(monkeypatch, r)
    assert q.proc_statuses() == []


def test_api_live_empty_and_points() -> None:
    from lquant.monitor.ring import api_ring
    from lquant.monitor.types import ApiMetricPoint

    api_ring.clear()
    assert q.api_live() == {"count": 0, "p50": None, "p95": None, "err_rate": 0.0}

    now = time.time()
    api_ring.append(ApiMetricPoint(ts=now - 1, route="/a", method="GET",
                                   status=200, duration_ms=10.0, dur_category="fast"))
    api_ring.append(ApiMetricPoint(ts=now - 1, route="/a", method="GET",
                                   status=500, duration_ms=30.0, dur_category="error"))
    api_ring.append(ApiMetricPoint(ts=now - 9999, route="/old", method="GET",
                                   status=200, duration_ms=5.0, dur_category="fast"))
    live = q.api_live()
    assert live["count"] == 2
    # pct 公式：int(qf*n)-1，n=2 时 p50/p95 都取排序后下标 0 → 10.0
    assert live["p50"] == 10.0
    assert live["p95"] == 10.0
    assert live["err_rate"] == pytest.approx(0.5)
    api_ring.clear()


def test_recent_task_events_no_redis_and_ok(monkeypatch) -> None:
    _disable_redis(monkeypatch)
    assert q.recent_task_events() == []
    r = _FakeRedis()
    _enable_redis(monkeypatch, r)
    out = q.recent_task_events(10)
    assert out == [{"event": "finished", "job": "j"}]


def test_recent_task_events_error_degrades(monkeypatch) -> None:
    _enable_redis(monkeypatch, _FakeRedis(fail=True))
    assert q.recent_task_events() == []


def test_data_pulls_recent_and_by_job(monkeypatch) -> None:
    from datetime import datetime

    started = datetime(2026, 9, 17, 9, 30)
    finished = datetime(2026, 9, 17, 9, 31)

    class _Con:
        def execute(self, sql):
            if "ORDER BY started_at DESC" in sql:
                return type("R", (), {"fetchall": staticmethod(lambda: [
                    ("sync", None, started, None, 5, "ok", None),
                    ("sync", "2026-09-17", started, finished, 5, "ok", None),
                ])})()
            return type("R", (), {"fetchall": staticmethod(lambda: [
                ("sync", 2, 30.0, 1),
                ("bad", 1, None, 1),
            ])})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import lquant.core.db

    @contextlib.contextmanager
    def fake_reader():
        yield _Con()

    monkeypatch.setattr(lquant.core.db, "reader", fake_reader)
    out = q.data_pulls()
    assert out["recent"][0]["duration_ms"] is None  # finished_at 为空
    assert out["recent"][1]["duration_ms"] == 60000.0
    assert out["by_job"][0]["avg_duration_ms"] == 30.0
    assert out["by_job"][1]["avg_duration_ms"] is None
    assert out["by_job"][1]["failed"] == 1
