"""monitor 查询层与 API 端点：分桶/聚合/快照/信封。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from lquant.monitor import queries as q
from lquant.monitor.ring import api_ring
from lquant.monitor.types import ApiMetricPoint


@pytest.fixture()
def mdb(tmp_path, monkeypatch):
    path = str(tmp_path / "mon.duckdb")
    monkeypatch.setenv("LQ_MONITOR_DB", path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    api_ring.drain()
    yield path
    get_settings.cache_clear()


def _seed_api(mdb, n=10):
    from datetime import datetime, timedelta

    con = duckdb.connect(mdb)
    from lquant.monitor.flusher import ensure_tables

    ensure_tables(con)
    base = datetime.now() - timedelta(minutes=2)
    for i in range(n):
        con.execute("INSERT INTO metrics_api VALUES (?, ?, ?, ?, ?, ?)",
                    [base + timedelta(seconds=i), "/api/factors", "GET",
                     200 if i < 8 else 500, 50.0 + i * 100,
                     "fast" if i < 8 else "error"])
    con.commit()
    con.close()


def test_range_bucket():
    assert q.range_bucket("1h") == 60
    assert q.range_bucket("6h") == 300
    assert q.range_bucket("24h") == 900
    assert q.range_bucket("7d") == 7200
    with pytest.raises(ValueError):
        q.range_bucket("9h")


def test_api_latency_series_buckets(mdb):
    _seed_api(mdb)
    rows = q.api_latency_series("1h")
    assert rows, "应有分桶输出"
    assert all({"bucket", "count", "avg", "p50", "p95", "err_rate"} == set(r) for r in rows)
    total = sum(r["count"] for r in rows)
    assert total == 10
    # err_rate 是每桶内的比率，跨桶求和必须按 count 加权——种子 9 秒跨度
    # 可能跨 60s 分桶边界，直接 sum 会随运行时刻漂移（曾致 0.2/0.5 随机摆动）
    assert sum(r["err_rate"] * r["count"] for r in rows) / total == pytest.approx(0.2)


def test_slowest_routes(mdb):
    _seed_api(mdb)
    rows = q.slowest_routes("1h")
    assert rows[0]["route"] == "/api/factors"
    assert rows[0]["count"] == 10


def test_proc_statuses_offline_and_online(mdb):
    fake = MagicMock()
    stale = time.time() - 60
    fresh = time.time()
    fake.scan_iter.return_value = [b"lquant:monitor:proc:general-0",
                                   b"lquant:monitor:proc:http"]
    fake.get.side_effect = [
        json.dumps({"ts": stale, "proc_name": "general-0", "pid": 1,
                    "cpu_pct": None, "mem_rss_mb": None, "current_job": None}),
        json.dumps({"ts": fresh, "proc_name": "http", "pid": 2,
                    "cpu_pct": 3.0, "mem_rss_mb": 100.0, "current_job": None}),
    ]
    with patch.object(q, "_redis_available", return_value=True), \
         patch.object(q, "_get_redis", return_value=fake):
        rows = q.proc_statuses()
    by = {r["proc_name"]: r for r in rows}
    assert by["general-0"]["online"] is False
    assert by["http"]["online"] is True
    assert by["http"]["cpu_pct"] == 3.0


def test_api_live_from_ring(mdb):
    api_ring.append(ApiMetricPoint(ts=time.time() - 10, route="/api/x",
                                   method="GET", status=200, duration_ms=50.0,
                                   dur_category="fast"))
    api_ring.append(ApiMetricPoint(ts=time.time() - 10, route="/api/x",
                                   method="GET", status=500, duration_ms=20.0,
                                   dur_category="error"))
    out = q.api_live()
    assert out["count"] == 2
    assert out["err_rate"] == pytest.approx(0.5)


def test_summary_endpoint_envelope(mdb):
    _seed_api(mdb)
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        assert "procs" in body["data"]
        assert "queues" in body["data"]
        assert "api_live" in body["data"]
        r2 = client.get("/api/monitor/api-latency?range=1h")
        assert r2.status_code == 200
        assert "series" in r2.json()["data"]
        assert "slowest" in r2.json()["data"]


def test_tasks_endpoint(mdb):
    from datetime import datetime, timedelta

    con = duckdb.connect(mdb)
    from lquant.monitor.flusher import ensure_tables

    ensure_tables(con)
    con.execute("INSERT INTO metrics_task VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [datetime.now(), "j1", "fn", "finished", "lquant-default",
                 datetime.now() - timedelta(seconds=5),
                 datetime.now() - timedelta(seconds=4),
                 datetime.now(), 1000.0, 1000.0, None])
    con.commit()
    con.close()
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/tasks?range=1h")
        assert r.status_code == 200
        rows = r.json()["data"]["series"]
        assert sum(x["count"] for x in rows) == 1


def test_data_pulls_endpoint(tmp_path, monkeypatch):
    """collect_log 视角：主库 reader() 读 recent + by_job。"""
    import os

    os.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "mon.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    with __import__("lquant.core.db", fromlist=["writer"]).writer() as con:
        con.execute("CREATE TABLE collect_log (job VARCHAR, trade_date DATE, "
                    "started_at TIMESTAMP, finished_at TIMESTAMP, rows INTEGER, "
                    "status VARCHAR, message VARCHAR, PRIMARY KEY (job, trade_date))")
        con.execute("INSERT INTO collect_log VALUES ('daily', '2026-09-01', "
                    "'2026-09-01 18:00:00', '2026-09-01 18:00:10', 100, 'ok', NULL)")
    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/data-pulls")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["recent"][0]["job"] == "daily"
        assert data["by_job"][0]["avg_duration_ms"] == 10000.0
    get_settings.cache_clear()
