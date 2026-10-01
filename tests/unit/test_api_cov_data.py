"""API 覆盖补齐：data 端点（gaps/repair、check、checkpoints、crosscheck、quote 等）。"""
from __future__ import annotations

import datetime
import os

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_cov_data")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    generate_demo(start="2025-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------- coverage / securities / daily ----------------

def test_coverage_and_ping(client):
    assert client.get("/api/data/ping").json() == {"pong": "data"}
    r = client.get("/api/data/coverage")
    assert r.status_code == 200
    assert r.json()["daily_lake"]["rows"] > 0


def test_coverage_monthly(client):
    r = client.get("/api/data/coverage/monthly")
    assert r.status_code == 200
    assert r.json()["rows"]
    assert client.get("/api/data/coverage/monthly",
                      params={"start": "bad-date"}).status_code == 422


def test_securities_filters(client):
    r = client.get("/api/data/securities", params={"q": "600519"})
    assert r.status_code == 200 and r.json()
    r2 = client.get("/api/data/securities", params={"sec_type": "etf"})
    assert r2.status_code == 200
    # 查询异常 → 空
    from contextlib import contextmanager

    from lquant.server.api import data as data_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "security" in sql and "LIKE" in sql:
                raise RuntimeError("boom")
            return self._con.execute(sql, *a, **k)

    real_reader = data_mod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    data_mod.reader = fake
    try:
        assert client.get("/api/data/securities", params={"q": "x"}).json() == []
    finally:
        data_mod.reader = real_reader


def test_daily_and_indicators(client):
    r = client.get("/api/data/daily", params={"symbol": "600519.SH", "limit": 5})
    assert r.status_code == 200 and len(r.json()) <= 5
    # 空结果
    assert client.get("/api/data/daily",
                      params={"symbol": "999999.SH"}).json() == []
    r2 = client.get("/api/data/indicators", params={"symbol": "600519.SH"})
    assert r2.status_code == 200 and r2.json()


def test_quote_degrades(client):
    r = client.get("/api/data/quote", params={"symbol": "600519.SH"})
    assert r.status_code == 200
    assert r.json()["available"] in (True, False)


# ---------------- gaps / repair ----------------

def _fake_rep(missing):
    return {
        "window": {"start": datetime.date(2026, 6, 1), "end": datetime.date(2026, 6, 30)},
        "tables": {
            "daily": {"missing_dates": missing, "sparse_symbols": []},
            "daily_basic": {"missing_dates": [], "sparse_symbols": []},
        },
    }


def test_gaps_endpoint(client, monkeypatch):
    from lquant.data.quality import coverage as cov_mod

    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair=False: _fake_rep(
                            [datetime.date(2026, 6, 5)]))
    r = client.get("/api/data/gaps")
    assert r.status_code == 200
    body = r.json()
    assert body["missing_dates"] == ["2026-0-05"] or body["missing_dates"]


def test_repair_gaps_no_gap(client, monkeypatch):
    from lquant.data.quality import coverage as cov_mod

    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair=False: _fake_rep([]))
    r = client.post("/api/data/gaps/repair", json={"days": 30})
    assert r.status_code == 202
    assert r.json() == {"created": False, "task_id": None, "reason": "no_gap"}


def test_repair_gaps_creates_task(client, monkeypatch):
    from lquant.core.db import writer
    from lquant.data.ingest import tasks as tasks_mod
    from lquant.data.quality import coverage as cov_mod
    from lquant.server.api import data as data_mod

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute("DELETE FROM data_task")
    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair=False: _fake_rep(
                            [datetime.date(2026, 6, 5)]))

    def _noop(task_id):
        from lquant.data.ingest.tasks import get_task

        return get_task(task_id)

    old_exec = data_mod.execute_task
    old_tasks_exec = tasks_mod.execute_task
    data_mod.execute_task = _noop
    tasks_mod.execute_task = _noop
    try:
        r = client.post("/api/data/gaps/repair", json={"days": 30})
        assert r.status_code == 202, r.text
        assert r.json()["created"] is True
        # 有活跃任务 → 冲突分支
        r2 = client.post("/api/data/gaps/repair", json={"days": 30})
        assert r2.status_code == 202
        assert r2.json()["reason"] == "active_task_exists"
    finally:
        data_mod.execute_task = old_exec
        tasks_mod.execute_task = old_tasks_exec
        with writer() as con:
            con.execute("DELETE FROM data_task")


def test_gaps_error_502(client, monkeypatch):
    from lquant.data.quality import coverage as cov_mod

    def _boom(days, repair=False):
        raise RuntimeError("scan down")

    monkeypatch.setattr(cov_mod, "scan_coverage", _boom)
    r = client.get("/api/data/gaps")
    assert r.status_code == 502


# ---------------- reference sync / index-cons / tasks ----------------

def test_reference_sync_202_and_409(client):
    from lquant.server.api import data as data_mod

    orig = data_mod.sync_reference
    calls = []

    def _fake(skip_details=True):
        calls.append(skip_details)

    # 锁被占用 → 409
    assert data_mod._ref_lock.acquire(blocking=False)
    try:
        assert client.post("/api/data/reference/sync",
                           json={}).status_code == 409
    finally:
        data_mod._ref_lock.release()
    # 空闲 → 202（stub 后台任务，不碰网络）
    data_mod.sync_reference = _fake
    try:
        r = client.post("/api/data/reference/sync", json={})
        assert r.status_code == 202
        assert r.json() == {"accepted": True, "sync_details": False}
    finally:
        data_mod.sync_reference = orig


def test_index_cons_sync(client, monkeypatch):
    from lquant.data.ingest import index_cons as ic_mod

    monkeypatch.setattr(ic_mod, "sync_index_cons", lambda indexes=None: {"n": 1})
    r = client.post("/api/data/index-cons/sync", json={})
    assert r.status_code == 200 and r.json() == {"n": 1}

    def _boom(indexes=None):
        raise RuntimeError("tushare down")

    monkeypatch.setattr(ic_mod, "sync_index_cons", _boom)
    assert client.post("/api/data/index-cons/sync", json={}).status_code == 502


def test_data_tasks_contract(client):
    from lquant.core.db import writer
    from lquant.data.ingest import tasks as tasks_mod

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute("DELETE FROM data_task")
    r = client.post("/api/data/tasks", json={"kind": "nope_kind"})
    assert r.status_code == 422
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"start": "2026-06-01",
                                                        "end": "2026-06-30"}})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    # pending → conflict 409
    assert client.post("/api/data/tasks", json={"kind": "daily_update",
                                                "params": {"start": "2026-06-01",
                                                           "end": "2026-06-30"}}).status_code == 409
    # 列表 / 详情
    assert client.get("/api/data/tasks").status_code == 200
    assert client.get(f"/api/data/tasks/{tid}").status_code == 200
    assert client.get("/api/data/tasks/ghost").status_code == 404
    # pending → retry 409
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 409
    # ok 终态 → retry 422
    with writer() as con:
        con.execute("UPDATE data_task SET status = 'ok' WHERE task_id = ?", [tid])
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 422
    with writer() as con:
        con.execute("DELETE FROM data_task")


def test_task_events_404(client):
    assert client.get("/api/data/tasks/ghost/events").status_code == 404


async def test_task_events_stream(api_env):
    """SSE 流：snapshot → progress → done 后收线（复用 test_task_events 模式）。"""
    import asyncio

    import httpx

    from lquant.core import task_events
    from lquant.data.ingest.tasks import create_task
    from lquant.server.main import create_app

    task_id = create_task(kind="daily_update", params={"days": 1})["task_id"]
    frames: list[bytes] = []

    async def publish_events() -> None:
        await asyncio.sleep(0.2)
        task_events.publish(task_id, {"task_id": task_id, "done": 3})
        task_events.publish(task_id, {"task_id": task_id, "terminal": True, "done": 10})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    ) as hx:
        pub = asyncio.create_task(publish_events())
        async with hx.stream("GET", f"/api/data/tasks/{task_id}/events") as r:
            assert r.status_code == 200
            async for chunk in r.aiter_bytes():
                frames.append(chunk)
                if b"event: done" in b"".join(frames):
                    break
        await asyncio.wait_for(pub, timeout=5)

    raw = b"".join(frames)
    assert b"event: snapshot" in raw
    assert b"event: progress" in raw
    assert b"event: done" in raw


# ---------------- check / checkpoints / crosscheck ----------------

def test_lake_check(client, monkeypatch):
    from lquant.data.quality import pipeline as pipe_mod
    from lquant.data.quality.issues import Issue

    issues = [
        Issue(rule="rule_a", severity="error", detail="3 行异常"),
        Issue(rule="rule_b", severity="info", detail="ok", dataset="daily_basic"),
    ]
    monkeypatch.setattr(pipe_mod, "run_lake_checks", lambda start=None, end=None: issues)
    r = client.post("/api/data/check", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["total"] == 2
    assert body["issues"][0]["severity"] == "error"     # error 排最前
    # 非法日期 → 422
    assert client.post("/api/data/check",
                       json={"start": "junk"}).status_code == 422


def test_lake_check_409_locked(client):
    from lquant.server.api import data as data_mod

    assert data_mod._lake_check_lock.acquire(blocking=False)
    try:
        r = client.post("/api/data/check", json={})
        assert r.status_code == 409
    finally:
        data_mod._lake_check_lock.release()


def test_checkpoints_lifecycle(client):
    import json as _json
    from pathlib import Path

    from lquant.core.config import get_settings

    d = Path(get_settings().cache_dir) / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    (d / "daily_update.json").write_text(
        _json.dumps({"done": ["600519.SH", "000001.SZ"],
                     "updated_at": "2026-06-30T15:00:00",
                     "meta": {"start": "2026-06-01"}}), encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "list.json").write_text("[1,2]", encoding="utf-8")

    r = client.get("/api/data/checkpoints")
    assert r.status_code == 200
    names = {c["name"] for c in r.json()}
    assert "daily_update" in names

    # 归档
    a = client.delete("/api/data/checkpoints/daily_update")
    assert a.status_code == 200
    assert a.json()["archived_to"].startswith("daily_update.done-")
    assert client.delete("/api/data/checkpoints/ghost").status_code == 404
    # 名称含 .. → 防路径穿越 422
    assert client.delete("/api/data/checkpoints/a..b").status_code == 422


def test_crosscheck_endpoints(client, monkeypatch):
    from lquant.data.ingest import crosscheck as cc_mod
    from lquant.data.quality.issues import Issue, ensure_table, save_issues

    def _ok(**k):
        return {"summary": {"n": 1}, "issues": [], "flagged_rows": []}

    monkeypatch.setattr(cc_mod, "run_crosscheck", _ok)
    r = client.post("/api/data/crosscheck", json={})
    assert r.status_code == 200 and r.json()["summary"]["n"] == 1

    def _boom(**k):
        raise ValueError("peers 参数非法")

    monkeypatch.setattr(cc_mod, "run_crosscheck", _boom)
    assert client.post("/api/data/crosscheck", json={}).status_code == 422

    # issues 检索 + resolve
    from lquant.core.db import writer

    with writer() as con:
        ensure_table(con)
    save_issues([Issue(rule="rule_x", severity="warn", detail="需要修")])
    r = client.get("/api/data/crosscheck/issues")
    assert r.status_code == 200
    hit = next(i for i in r.json() if i["rule_code"] == "rule_x")
    assert client.post("/api/data/crosscheck/issues/resolve",
                       json={"issue_id": hit["issue_id"]}).status_code == 200
    assert client.post("/api/data/crosscheck/issues/resolve",
                       json={"issue_id": "ghost"}).status_code == 404


def test_retry_claim_error_branches(client, monkeypatch):
    """claim_retry ValueError/TaskConflictError → 422/409；成功路径。"""
    from lquant.core.db import writer
    from lquant.data.ingest import tasks as tasks_mod
    from lquant.data.ingest.tasks import TaskConflictError
    from lquant.server.api import data as data_mod

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute("DELETE FROM data_task")
    tid = client.post("/api/data/tasks", json={"kind": "daily_update",
                                               "params": {"start": "2026-06-01",
                                                          "end": "2026-06-30"}}).json()["task_id"]
    # pending → 409
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 409
    with writer() as con:
        con.execute("UPDATE data_task SET status = 'failed' WHERE task_id = ?", [tid])

    def _ok(task_id):
        return True

    def _value_error(task_id):
        raise ValueError("终态 ok 不可 retry")

    def _conflict(task_id):
        raise TaskConflictError("竞争抢先")

    old = tasks_mod.claim_retry
    old_rc = tasks_mod.run_claimed_task
    old_dr = data_mod.claim_retry
    old_drc = data_mod.run_claimed_task

    def _noop(task_id):
        return None

    for stub, expected in ((_value_error, 422), (_conflict, 409), (_ok, 202)):
        tasks_mod.claim_retry = stub
        data_mod.claim_retry = stub
        tasks_mod.run_claimed_task = _noop
        data_mod.run_claimed_task = _noop
        r = client.post(f"/api/data/tasks/{tid}/retry")
        assert r.status_code == expected, r.text
    tasks_mod.claim_retry = old
    tasks_mod.run_claimed_task = old_rc
    data_mod.claim_retry = old_dr
    data_mod.run_claimed_task = old_drc
    with writer() as con:
        con.execute("DELETE FROM data_task")


# ---------------- 补充分支（日期校验 / 空湖 / quote 打桩 / retry / issue 异常） ----------------

def test_crosscheck_input_date_validation(client):
    """CrosscheckIn._validate_date：非法 start/end → 422。"""
    r = client.post("/api/data/crosscheck", json={"start": "junk"})
    assert r.status_code == 422
    r2 = client.post("/api/data/crosscheck", json={"end": "2026/01/02"})
    assert r2.status_code == 422


def test_coverage_table_and_lake_error_branches(client, monkeypatch):
    """单表坏了不影响整页；湖异常 → error 标记；空 schema → rows: []。"""
    from contextlib import contextmanager

    from lquant.server.api import data as data_mod

    class _Proxy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **k):
            if "DESCRIBE" in sql or "count(*)" in sql:
                raise RuntimeError("table broken")
            return self._con.execute(sql, *a, **k)

    real_reader = data_mod.reader

    @contextmanager
    def fake():
        with real_reader() as con:
            yield _Proxy(con)

    monkeypatch.setattr(data_mod, "reader", fake)
    r = client.get("/api/data/coverage")
    assert r.status_code == 200
    bad = [t for t in r.json()["tables"] if t.get("error")]
    assert bad                                    # 坏表 → error 条目

    # 湖异常 → daily_lake.error = True
    def _boom(*a, **k):
        raise RuntimeError("lake down")

    monkeypatch.setattr(data_mod, "read_daily", _boom)
    r2 = client.get("/api/data/coverage")
    assert r2.status_code == 200
    assert r2.json()["daily_lake"]["error"] is True

    # coverage/monthly：湖异常 → 降级空 rows
    r3 = client.get("/api/data/coverage/monthly")
    assert r3.status_code == 200
    assert r3.json()["rows"] == []

    # coverage/monthly：空 schema → rows: []（非异常路径）
    monkeypatch.setattr(data_mod, "read_daily", lambda *a, **k: pl.DataFrame().lazy())
    r4 = client.get("/api/data/coverage/monthly")
    assert r4.status_code == 200
    assert r4.json()["rows"] == []


def test_indicators_empty_lake(client, monkeypatch):
    """指标端点空湖 → []。"""
    from lquant.server.api import data as data_mod

    empty = pl.DataFrame(schema={"trade_date": pl.Date, "symbol": pl.String,
                                 "close": pl.Float64, "open": pl.Float64,
                                 "high": pl.Float64, "low": pl.Float64,
                                 "volume": pl.Float64})
    monkeypatch.setattr(data_mod, "read_daily", lambda *a, **k: empty.lazy())
    r = client.get("/api/data/indicators", params={"symbol": "600519.SH"})
    assert r.status_code == 200 and r.json() == []


def test_quote_stubbed_success_and_deep_codes(client, monkeypatch):
    """quote 成功路径（打桩 em_get）+ 深市 secid 分支。"""
    from lquant.market import em_client as em_mod

    class _R:
        def json(self):
            return {"data": {"f57": "600519", "f58": "贵州茅台", "f43": 1500.0,
                             "f169": 12.0, "f170": 0.8, "f46": 1490.0,
                             "f44": 1510.0, "f45": 1480.0, "f60": 1488.0,
                             "f47": 1000, "f48": 1.5e8, "f50": 0.6,
                             "f116": 1.8e12}}

    captured = {}

    def _fake(url, qps=None):
        captured["url"] = url
        return _R()

    monkeypatch.setattr(em_mod, "em_get", _fake)
    r = client.get("/api/data/quote", params={"symbol": "600519.SH"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["name"] == "贵州茅台"
    assert "secid=1.600519" in captured["url"]      # 沪市
    r2 = client.get("/api/data/quote", params={"symbol": "000001.SZ"})
    assert r2.status_code == 200 and r2.json()["available"] is True
    assert "secid=0.000001" in captured["url"]      # 深市
    # f57 缺失 → available False
    monkeypatch.setattr(em_mod, "em_get",
                        lambda url, qps=None: type("R2", (), {
                            "json": staticmethod(lambda: {"data": {}})})())
    r3 = client.get("/api/data/quote", params={"symbol": "600519.SH"})
    assert r3.json()["available"] is False
