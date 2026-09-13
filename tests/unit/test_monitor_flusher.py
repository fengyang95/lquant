"""flusher：建表、落盘、事件 drain、样本落盘、保留清理、写失败重试。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from lquant.monitor import flusher as fl
from lquant.monitor.ring import api_ring, local_events
from lquant.monitor.types import ApiMetricPoint, TaskEvent


@pytest.fixture()
def mdb(tmp_path, monkeypatch):
    """独立 monitor.duckdb + 环缓冲清空。"""
    path = str(tmp_path / "mon.duckdb")
    monkeypatch.setenv("LQ_MONITOR_DB", path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    api_ring.drain()
    local_events.drain()
    yield path
    get_settings.cache_clear()


def _pt(ts=1000.0, route="/api/factors", status=200, dur=50.0):
    return ApiMetricPoint(ts=ts, route=route, method="GET", status=status,
                          duration_ms=dur, dur_category=fl._classify(status, dur)
                          if hasattr(fl, "_classify") else "fast")


def test_ensure_tables_idempotent(mdb):
    con = duckdb.connect(mdb)
    fl.ensure_tables(con)
    fl.ensure_tables(con)  # 二次不炸
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"metrics_api", "metrics_task", "metrics_sys"} <= names
    con.close()


def test_flush_api_points(mdb):
    api_ring.drain()
    api_ring.append(_pt(ts=time.time() - 5))
    api_ring.append(ApiMetricPoint(ts=time.time() - 4, route="/api/x",
                                   method="POST", status=500, duration_ms=9.0,
                                   dur_category="error"))
    out = fl.flush_once()
    assert out["api"] == 2
    con = duckdb.connect(mdb)
    n, err = con.execute(
        "SELECT count(*), sum(CASE WHEN dur_category='error' THEN 1 ELSE 0 END) "
        "FROM metrics_api").fetchone()
    con.close()
    assert n == 2 and err == 1
    assert api_ring.snapshot() == ()  # 成功落盘后清空


def test_flush_local_task_events(mdb):
    local_events.drain()
    local_events.put(TaskEvent(
        event_ts=time.time(), job_id="j1", job_name="fn", event="finished",
        queue="lquant-default", enqueued_at=100.0, started_at=110.0,
        finished_at=120.0, elapsed_ms=10000.0, queue_delay_ms=10000.0,
        message=None))
    out = fl.flush_once()
    assert out["task"] >= 1
    con = duckdb.connect(mdb)
    row = con.execute("SELECT job_id, elapsed_ms FROM metrics_task "
                      "WHERE event='finished'").fetchone()
    con.close()
    assert row == ("j1", 10000.0)


def test_flush_redis_events_fifo(mdb):
    """Redis events list：LPUSH 写入的 payload，flusher RPOP FIFO drain。"""
    con = duckdb.connect(mdb)
    con.close()
    fake = MagicMock()
    payloads = [json.dumps({"event_ts": 1.0, "job_id": f"j{i}",
                            "job_name": "fn", "event": "finished",
                            "queue": "q", "enqueued_at": None,
                            "started_at": None, "finished_at": None,
                            "elapsed_ms": 1.0, "queue_delay_ms": None,
                            "message": None}) for i in (1, 2)]
    # RPOP 顺序 = FIFO：j1 先出
    fake.rpop.side_effect = [payloads[0], payloads[1], None]
    with patch.object(fl, "_redis_available", return_value=True), \
         patch.object(fl, "_get_redis", return_value=fake):
        out = fl.flush_once()
    assert out["task"] == 2
    got = duckdb.connect(mdb).execute(
        "SELECT job_id FROM metrics_task ORDER BY event_ts, job_id").fetchall()
    assert [r[0] for r in got] == ["j1", "j2"]


def test_flush_sys_samples(mdb):
    con = duckdb.connect(mdb)
    con.close()
    fake = MagicMock()
    fake.scan_iter.return_value = [b"lquant:monitor:proc:http"]
    fake.get.return_value = json.dumps({"ts": time.time(), "proc_name": "http",
                                        "pid": 1, "cpu_pct": 1.5,
                                        "mem_rss_mb": 88.0,
                                        "current_job": None})
    with patch.object(fl, "_redis_available", return_value=True), \
         patch.object(fl, "_get_redis", return_value=fake):
        out = fl.flush_once()
    assert out["sys"] == 1
    row = duckdb.connect(mdb).execute(
        "SELECT proc_name, cpu_pct FROM metrics_sys").fetchone()
    assert row == ("http", 1.5)


def test_write_failure_keeps_ring(mdb, monkeypatch):
    api_ring.drain()
    api_ring.append(_pt())
    # 用不存在的父目录制造写失败
    monkeypatch.setenv("LQ_MONITOR_DB", "/nonexistent-dir-zz/x.duckdb")
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    out = fl.flush_once()  # 不应抛出
    assert out["api"] == 0
    assert len(api_ring.snapshot()) == 1  # 数据保留待重试
    get_settings.cache_clear()


def test_write_failure_keeps_data_for_retry(mdb):
    """写阶段失败：已 drain 的数据暂存 pending，下个周期成功重写。"""
    api_ring.drain()
    local_events.drain()
    with fl._PENDING_LOCK:
        fl._PENDING_API.clear()
        fl._PENDING_TASK.clear()
    api_ring.append(_pt(ts=time.time()))
    real_write_api = fl._write_api
    state = {"n": 0}

    def flaky(con, pts):
        if state["n"] == 0:
            state["n"] += 1
            raise RuntimeError("boom")
        return real_write_api(con, pts)

    with patch.object(fl, "_write_api", side_effect=flaky):
        out = fl.flush_once()
    assert out["api"] == 0
    assert api_ring.snapshot() == ()  # 已 drain，由 pending 保留
    out2 = fl.flush_once()
    assert out2["api"] == 1  # 下个周期 pending 重写成功
    n = duckdb.connect(mdb).execute(
        "SELECT count(*) FROM metrics_api").fetchone()[0]
    assert n == 1


def test_retention_cleanup(mdb):
    con = duckdb.connect(mdb)
    fl.ensure_tables(con)
    old = time.time() - 30 * 86400
    con.execute("INSERT INTO metrics_api VALUES (?, 'r', 'GET', 200, 1.0, 'fast')",
                [__import__("datetime").datetime.fromtimestamp(old)])
    con.close()
    with patch.object(fl, "_should_cleanup_today", return_value=True):
        fl.flush_once()
    n = duckdb.connect(mdb).execute("SELECT count(*) FROM metrics_api").fetchone()[0]
    assert n == 0


def test_ensure_tables_contains_error_table(mdb):
    con = duckdb.connect(mdb)
    fl.ensure_tables(con)
    names = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    con.close()
    assert "metrics_api_error" in names


def test_flush_error_points(mdb):
    from lquant.monitor.ring import error_ring
    from lquant.monitor.types import ApiErrorPoint

    error_ring.drain()
    error_ring.append(ApiErrorPoint(
        ts=time.time(), route="/api/factors/evaluate", method="POST",
        status=500, error_type="DSLParseError", message="表达式意外结束",
        traceback_tail="Traceback ... DSLParseError: 表达式意外结束"))
    out = fl.flush_once()
    assert out["error"] == 1
    con = duckdb.connect(mdb)
    row = con.execute(
        "SELECT route, status, error_type, message FROM metrics_api_error"
    ).fetchone()
    con.close()
    assert row[0] == "/api/factors/evaluate" and row[1] == 500
    assert row[2] == "DSLParseError" and row[3] == "表达式意外结束"
    assert error_ring.snapshot() == ()


def test_error_write_failure_keeps_pending(mdb, monkeypatch):
    """错误日志写失败同样走 pending 重试，不丢数据。"""
    from lquant.monitor.ring import error_ring
    from lquant.monitor.types import ApiErrorPoint

    error_ring.drain()
    with fl._PENDING_LOCK:
        fl._PENDING_ERRORS.clear()
    error_ring.append(ApiErrorPoint(
        ts=time.time(), route="/api/x", method="GET", status=500,
        error_type="ValueError", message="boom", traceback_tail=None))
    real = fl._write_errors
    state = {"n": 0}

    def flaky(con, pts):
        if state["n"] == 0:
            state["n"] += 1
            raise RuntimeError("boom")
        return real(con, pts)

    with patch.object(fl, "_write_errors", side_effect=flaky):
        fl.flush_once()
    assert error_ring.snapshot() == ()  # 已 drain，由 pending 保留
    out2 = fl.flush_once()
    assert out2["error"] == 1
    n = duckdb.connect(mdb).execute("SELECT count(*) FROM metrics_api_error").fetchone()[0]
    assert n == 1


def test_retention_cleanup_error_table(mdb):
    con = duckdb.connect(mdb)
    fl.ensure_tables(con)
    old = __import__("datetime").datetime.fromtimestamp(time.time() - 30 * 86400)
    con.execute("INSERT INTO metrics_api_error VALUES (?, 'r', 'GET', 500, 'T', 'm', NULL)",
                [old])
    con.close()
    with patch.object(fl, "_should_cleanup_today", return_value=True):
        fl.flush_once()
    n = duckdb.connect(mdb).execute("SELECT count(*) FROM metrics_api_error").fetchone()[0]
    assert n == 0
