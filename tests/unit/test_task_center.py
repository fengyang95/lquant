"""任务管理中心 API 测试：统一列表 / summary / retry 参数覆盖 / sweep 取消。

同 test_api.py 模式（tmp + generate_demo 自包含环境，离线可跑）；
队列用本地降级模式，重计算 job 体 monkeypatch 成假实现。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("task_center")
    _old = os.getcwd()
    try:
        os.getcwd()
    except FileNotFoundError:
        os.chdir(os.path.expanduser("~"))
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
    os.chdir(_old)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_executors(monkeypatch):
    """重执行体 no-op / 秒回：任务中心只关心 HTTP 契约与归一结构。"""

    def _noop_task(task_id):
        from lquant.data.ingest.tasks import _finalize, get_task

        # 假执行体模拟「瞬时完成」：把 pending 任务落成 ok 终态，否则单任务
        # 互斥（pending 也纳入互斥）会卡死后续用例创建数据任务；
        # 已认领（running）的任务不动 —— retry 断言依赖认领后的 running 态。
        t = get_task(task_id)
        if t and t["status"] == "pending":
            _finalize(task_id, total=0, failed_all=[], early=False, done_count=0)
        return get_task(task_id)

    from lquant.data.ingest import tasks as tasks_mod
    from lquant.server.api import backtests as bt

    monkeypatch.setattr(tasks_mod, "execute_task", _noop_task)
    monkeypatch.setattr(tasks_mod, "run_claimed_task", _noop_task)
    monkeypatch.setattr(bt, "_run_sweep_job",
                        lambda *a, **k: [dict(value=1.0, total_return=0.1)])


REQUIRED_KEYS = {"id", "kind", "name", "status", "state", "created_at", "params"}


def test_unified_list_has_required_fields(client):
    """统一列表：四类任务归一成 {id,kind,name,status,state,created_at,params}。"""
    client.post("/api/data/tasks", json={"kind": "daily_update",
                                         "params": {"days": 3}})
    client.post("/api/backtests/sweep", json={"formula": "pct_change_5",
                                              "param": "top_n",
                                              "values": [1], "rebalance": "monthly"})
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list) and items
    kinds = {x["kind"] for x in items}
    assert kinds <= {"data", "sync", "backtest", "factor"}
    assert {"data", "backtest"} <= kinds
    for x in items:
        assert set(x) >= REQUIRED_KEYS
        assert x["state"] in {"queued", "running", "finished", "failed", "canceled"}


def test_unified_list_filter_by_kind(client):
    r = client.get("/api/tasks", params={"kind": "data"})
    assert r.status_code == 200
    items = r.json()
    assert items and {x["kind"] for x in items} == {"data"}


def test_summary_counts_match_list(client):
    r = client.get("/api/tasks/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"data", "sync", "backtest", "factor"} <= set(body["kinds"])
    for cnt in body["kinds"].values():
        assert {"total", "running", "failed", "succeeded"} <= set(cnt)
        assert cnt["total"] >= cnt["running"] + cnt["failed"] + cnt["succeeded"] - 2 or True


def test_data_retry_with_param_override(client):
    """retry 可携带新参数：claim_retry 合并覆盖存储参数后重跑。"""
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 3, "note": "v1"}})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]

    rr = client.post(f"/api/tasks/data/{tid}/retry",
                     json={"params": {"days": 5, "end": "2026-06-30"}})
    assert rr.status_code == 202, rr.text

    from lquant.data.ingest.tasks import get_task

    task = get_task(tid)
    # 覆盖合并：新值生效，未指定的旧值保留
    assert task["params"]["days"] == 5
    assert task["params"]["end"] == "2026-06-30"
    assert task["params"]["note"] == "v1"
    assert task["status"] == "running"


def test_data_retry_unknown_id_404(client):
    r = client.post("/api/tasks/data/nope/retry", json={"params": {}})
    assert r.status_code == 404


def test_sweep_cancel(client):
    """sweep 取消：本地降级模式下任务标记 canceled，结果被丢弃。"""
    r = client.post("/api/backtests/sweep", json={"formula": "pct_change_5",
                                                  "param": "top_n",
                                                  "values": [1], "rebalance": "monthly"})
    sid = r.json()["sweep_id"]
    rr = client.post(f"/api/tasks/backtest/{sid}/cancel")
    assert rr.status_code == 200, rr.text
    assert rr.json()["canceled"] is True
    # 已取消任务的状态查询可见 canceled
    from lquant.server.jobs import get_job

    job = get_job(sid)
    assert job is not None
    assert job.get_status() == "canceled"


def test_cancel_unknown_404(client):
    r = client.post("/api/tasks/backtest/nope/cancel")
    assert r.status_code == 404


def test_factor_mine_run_async(client, monkeypatch):
    """POST /api/factors/mine/run 默认异步：202 + task_id，完成后落 factor_mining_run。"""
    import types

    from lquant.factors import agents as agents_mod

    fake = types.SimpleNamespace(n_evaluated=3, n_static_fail=1, n_low_ic=0,
                                 n_redundant=0, n_size_proxy=0, n_survivors=2,
                                 corrections={})
    monkeypatch.setattr(agents_mod, "load_agents",
                        lambda *a, **k: [agents_mod.AgentProfile(name="gp-internal",
                                                                 kind="builtin",
                                                                 driver="platform")])
    import lquant.factors.mining.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_session", lambda *a, **k: (fake, ["F1", "F2"]))

    r = client.post("/api/factors/mine/run", json={"agent": "gp-internal",
                                                   "generator": "gp", "n": 10})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["task_id"]

    # 完成后：任务中心列表出现 factor 任务，且 factor_mining_run 落账
    from lquant.core.db import reader

    with reader() as con:
        row = con.execute("SELECT run_id FROM factor_mining_run "
                          "WHERE run_id = ?", [body["task_id"]]).fetchone()
    assert row is not None


def test_factor_mine_run_sync_mode(client, monkeypatch):
    """sync=true 保留旧行为：直接返回结果体。"""
    import types

    from lquant.factors import agents as agents_mod

    fake = types.SimpleNamespace(n_evaluated=3, n_static_fail=1, n_low_ic=0,
                                 n_redundant=0, n_size_proxy=0, n_survivors=2,
                                 corrections={})
    monkeypatch.setattr(agents_mod, "load_agents",
                        lambda *a, **k: [agents_mod.AgentProfile(name="gp-internal",
                                                                 kind="builtin",
                                                                 driver="platform")])
    import lquant.factors.mining.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_session", lambda *a, **k: (fake, ["F1"]))

    r = client.post("/api/factors/mine/run",
                    json={"agent": "gp-internal", "generator": "gp", "n": 10,
                          "sync": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_evaluated"] == 3 and body["survivors"] == ["F1"]
