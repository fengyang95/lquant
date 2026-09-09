"""API 层：/api/data/tasks CRUD + retry + crosscheck 端点 + WS data_task 兜底。

同 test_api.py 模式：tmp 目录 + generate_demo 自包含合成环境，全链路离线。
后台执行器（execute_task/retry_task）打桩为 no-op —— 队列行为在 T4 的
executor 测试覆盖，这里只测 HTTP 契约（状态码 / 封套 / 兜底帧）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_tasks")
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_executor(monkeypatch):
    """后台执行器 no-op：不让 demo 环境真去拉网络日线。"""

    def _noop(task_id):
        from lquant.data.ingest.tasks import get_task

        return get_task(task_id)

    from lquant.data.ingest import tasks as tasks_mod
    from lquant.server.api import data as data_mod

    monkeypatch.setattr(tasks_mod, "execute_task", _noop)
    monkeypatch.setattr(tasks_mod, "retry_task", _noop)
    # data.py `from ... import execute_task` 持有引用，须一并打桩
    monkeypatch.setattr(data_mod, "execute_task", _noop)
    monkeypatch.setattr(data_mod, "run_claimed_task", _noop)
    yield


def _seed_running_task() -> str:
    """直接落一行 running 任务（绕过 create_task 的互斥校验）。"""
    import uuid

    from lquant.core.db import writer

    tid = uuid.uuid4().hex[:12]
    with writer() as con:
        from lquant.data.ingest.tasks import _DDL

        con.execute(_DDL)
        con.execute(
            "INSERT INTO data_task (task_id, kind, params, status, "
            "total_symbols, done_symbols, failed_symbols, failed_detail, "
            "rows_written) VALUES (?, 'daily_update', '{}'::JSON, 'running', "
            "10, 3, '[]'::JSON, '[]'::JSON, 0)",
            [tid],
        )
    return tid


def _set_status(task_id: str, status: str) -> None:
    from lquant.core.db import writer

    with writer() as con:
        from lquant.data.ingest.tasks import _DDL

        con.execute(_DDL)
        con.execute("UPDATE data_task SET status=? WHERE task_id=?",
                    [status, task_id])


# ---------- tasks CRUD ----------

def test_create_and_get_task(client):
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 5}})
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    assert task_id
    r2 = client.get(f"/api/data/tasks/{task_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["kind"] == "daily_update"
    assert body["status"] == "pending"
    assert body["total_symbols"] > 0


def test_create_task_422_unknown_kind(client):
    r = client.post("/api/data/tasks", json={"kind": "bogus"})
    assert r.status_code == 422
    assert "未知任务类型" in r.json()["detail"]


def test_create_task_422_full_backfill_no_delisted(client):
    """demo 环境无退市股 → full_backfill 前置校验失败 422。"""
    r = client.post("/api/data/tasks", json={"kind": "full_backfill"})
    assert r.status_code == 422
    assert "退市" in r.json()["detail"]


def test_create_task_409_when_running(client):
    tid = _seed_running_task()
    try:
        r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                                 "params": {"days": 5}})
        assert r.status_code == 409
        assert "运行中" in r.json()["detail"]
    finally:
        _set_status(tid, "failed")


def test_list_tasks(client):
    lst = client.get("/api/data/tasks").json()
    assert isinstance(lst, list) and len(lst) >= 1
    assert {"task_id", "kind", "status"} <= set(lst[0])


def test_get_task_404(client):
    assert client.get("/api/data/tasks/no_such_task").status_code == 404


def test_retry_endpoint(client):
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 5}})
    tid = r.json()["task_id"]
    # pending → 不可 retry
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 409
    _set_status(tid, "partial")
    r2 = client.post(f"/api/data/tasks/{tid}/retry")
    assert r2.status_code == 202, r2.text
    assert r2.json()["task_id"] == tid
    assert client.post("/api/data/tasks/ghost/retry").status_code == 404
    # ok 状态不可 retry → 422
    _set_status(tid, "ok")
    assert client.post(f"/api/data/tasks/{tid}/retry").status_code == 422


# ---------- crosscheck ----------

def test_crosscheck_endpoints(client):
    r = client.post("/api/data/crosscheck", json={"limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "summary" in body and "flagged_rows" in body

    # 空 issue 表 → 空列表不 500
    empty = client.get("/api/data/crosscheck/issues")
    assert empty.status_code == 200

    from lquant.data.quality.issues import Issue, latest_issues, save_issues

    save_issues([Issue(rule="CROSS_SRC_DIFF.akshare.L2", severity="warn",
                       detail="对拍 L2：600519.SH@2026-01-05",
                       symbol="600519.SH")])
    lst = client.get("/api/data/crosscheck/issues").json()
    assert lst and lst[0]["rule_code"].startswith("CROSS_SRC_DIFF")
    issue_id = lst[0]["issue_id"]

    # resolve：成功 + 404
    ok = client.post("/api/data/crosscheck/issues/resolve",
                     json={"issue_id": issue_id})
    assert ok.status_code == 200 and ok.json()["resolved"] is True
    assert latest_issues(resolved=True)[0]["resolved"] is True
    missing = client.post("/api/data/crosscheck/issues/resolve",
                          json={"issue_id": "ghost"})
    assert missing.status_code == 404


# ---------- WS data_task 兜底 ----------

def test_ws_falls_back_to_data_task(client):
    """job 不存在但 data_task 命中 → 推 task 状态帧，终态一帧后关连。"""
    import uuid

    from lquant.core.db import writer

    tid = uuid.uuid4().hex[:12]
    with writer() as con:
        from lquant.data.ingest.tasks import _DDL

        con.execute(_DDL)
        con.execute(
            "INSERT INTO data_task (task_id, kind, params, status, phase, "
            "total_symbols, done_symbols, failed_symbols, failed_detail, "
            "rows_written) VALUES (?, 'daily_update', '{}'::JSON, 'ok', "
            "'etf', 10, 10, '[]'::JSON, '[]'::JSON, 100)",
            [tid],
        )
    with client.websocket_connect(f"/ws/jobs/{tid}") as ws:
        msg = ws.receive_json()
    assert msg["job_id"] == tid
    assert msg["status"] == "ok"
    assert msg["progress"] == {"done": 10, "total": 10, "phase": "etf"}
    assert msg["done"] is True


def test_ws_not_found_preserved(client):
    """data_task 也查不到 → 保持既有 not_found 契约。"""
    with client.websocket_connect("/ws/jobs/ghost-task") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "not_found" and msg["done"] is True


# ---------- 启动标记 ----------

def test_startup_marks_interrupted(client):
    """重启标记：启动钩子把 pending/running 残留标 interrupted。"""
    from lquant.data.ingest.tasks import get_task

    tid = _seed_running_task()
    from lquant.server import main as main_mod

    # startup 钩子挂在模块级 app 上（create_app() 新实例不注册钩子）
    with TestClient(main_mod.app) as c2:
        c2.get("/api/health/ping")
    task = get_task(tid)
    assert task["status"] == "interrupted"
    assert "retry" in task["message"]
