"""错误日志查询层 + /monitor/error-logs 端点测试。"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pytest

from lquant.monitor import queries as q
from lquant.monitor.ring import error_ring


@pytest.fixture()
def mdb(tmp_path, monkeypatch):
    path = str(tmp_path / "mon.duckdb")
    monkeypatch.setenv("LQ_MONITOR_DB", path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    error_ring.drain()
    yield path
    get_settings.cache_clear()


def _seed_errors(mdb, n=3):
    con = duckdb.connect(mdb)
    from lquant.monitor.flusher import ensure_tables

    ensure_tables(con)
    base = datetime.now() - timedelta(minutes=2)
    for i in range(n):
        con.execute("INSERT INTO metrics_api_error VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base + timedelta(seconds=i),
                     "/api/factors/evaluate" if i == 0 else "/api/other",
                     "POST" if i == 0 else "GET",
                     500,
                     "DSLParseError" if i == 0 else "ValueError",
                     "表达式意外结束" if i == 0 else "boom",
                     "Traceback ... DSLParseError" if i == 0 else None])
    con.commit()
    con.close()


def test_error_logs_query_filters_route(mdb):
    _seed_errors(mdb)
    out = q.error_logs("1h")
    assert out["total"] == 3
    assert out["items"][0]["route"] == "/api/other"  # ts DESC
    by_route = q.error_logs("1h", route="factors")
    assert by_route["total"] == 1
    assert by_route["items"][0]["error_type"] == "DSLParseError"


def test_error_logs_query_empty_db(mdb):
    out = q.error_logs("24h")
    assert out["items"] == [] and out["total"] == 0


def test_error_logs_missing_db_degrades_empty(mdb):
    """monitor.duckdb 存在但无表（首次运行）→ 降级空集不炸。"""
    import duckdb as _duckdb

    _duckdb.connect(mdb).close()  # 建库不建表
    out = q.error_logs("1h")
    assert out == {"items": [], "total": 0}


def test_error_logs_unwritable_db_degrades_empty(tmp_path, monkeypatch):
    """db 连接失败（路径不可写）也降级空集，不炸 500 —— _con() 须在 try 内。"""
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "no-such-dir" / "x.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    out = q.error_logs("1h")
    get_settings.cache_clear()
    assert out == {"items": [], "total": 0}


def test_error_logs_endpoint(mdb):
    _seed_errors(mdb)
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as client:
        r = client.get("/api/monitor/error-logs?range=1h")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total"] == 3
        assert data["items"][0]["status"] == 500

        r2 = client.get("/api/monitor/error-logs?range=1h&route=factors")
        assert r2.status_code == 200
        assert r2.json()["data"]["total"] == 1

        r3 = client.get("/api/monitor/error-logs?range=bad")
        assert r3.status_code == 422
