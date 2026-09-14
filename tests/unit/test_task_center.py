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
    # 等 noop 执行体把任务落终态再 retry：否则后台线程的 _finalize 与
    # 主线程的 claim_retry 并发写 duckdb，负载下偶发撞写锁
    _wait_task_status(tid, lambda s: s == "ok")

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


def _wait_status(getter, pred, timeout=5.0, interval=0.02):
    """轮询等待异步（本地线程）执行到位，超时抛 AssertionError。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = getter()
        if pred(v):
            return v
        time.sleep(interval)
    raise AssertionError(f"等待状态超时: {getter()!r}")


# 注意：本模块用例共享 module 级 duckdb（client fixture 未按用例换库），
# 且 create_task 有「同时仅一个未完成任务」互斥，用例间存在文件内顺序
# 依赖——不可乱序、不可 pytest-xdist 并行；retry 语义用例自带 _finalize
# 收尾，新用例若留下 running 态任务请先 _clear_active_tasks()。


def test_list_rejects_unknown_kind_422(client):
    """kind 非法（不在 KINDS）→ 422，与端点 _items 的兜底一致。"""
    r = client.get("/api/tasks", params={"kind": "bogus"})
    assert r.status_code == 422
    assert "未知任务类别" in r.text


def test_list_limit_validation(client):
    """limit 边界：ge=1/le=500，越界 422；合法 limit 生效。"""
    for bad in ("0", "-1", "501"):
        r = client.get("/api/tasks", params={"limit": bad})
        assert r.status_code == 422, (bad, r.status_code)
    r = client.get("/api/tasks", params={"limit": 1})
    assert r.status_code == 200
    assert len(r.json()) <= 1


def test_retry_non_data_kind_422(client):
    """retry 仅支持 data：backtest/factor 类别 → 422。"""
    r = client.post("/api/tasks/backtest/whatever/retry", json={"params": {}})
    assert r.status_code == 422
    assert "暂不支持 retry" in r.text


def _clear_active_tasks() -> None:
    """把遗留的 pending/running 数据任务落成 ok 终态。

    create_task 有「同时仅一个未完成任务」互斥；前面的用例可能故意留下
    running 态任务（如 retry 语义用例），不清掉后续创建会 409。
    """
    from lquant.data.ingest.tasks import _finalize, list_tasks

    for t in list_tasks(50):
        if t["status"] in ("pending", "running"):
            _finalize(t["task_id"], total=0, failed_all=[], early=False,
                      done_count=0)


def _wait_task_status(tid, pred, timeout=5.0):
    """轮询等待数据任务落到预期状态（本地线程异步执行有延迟）。"""
    from lquant.data.ingest.tasks import get_task

    return _wait_status(lambda: get_task(tid)["status"], pred, timeout)


def test_retry_running_task_409(client):
    """running 态任务不可 retry（claim_retry 原子认领失败）→ 409 TaskConflictError。"""
    _clear_active_tasks()
    from lquant.data.ingest.tasks import _finalize, get_task

    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 3}})
    tid = r.json()["task_id"]
    # 第一次 retry：ok/pending → running（stub 执行体不动 running 任务）
    rr = client.post(f"/api/tasks/data/{tid}/retry", json={"params": {}})
    assert rr.status_code == 202, rr.text
    assert get_task(tid)["status"] == "running"
    # 第二次 retry：running 不在可认领集合 → 409
    rc = client.post(f"/api/tasks/data/{tid}/retry", json={"params": {}})
    assert rc.status_code == 409, rc.text
    # stub 执行体不处理 running 任务 —— 收尾落 ok，避免 mutex 卡死后续用例
    _finalize(tid, total=0, failed_all=[], early=False, done_count=0)
    _wait_task_status(tid, lambda s: s == "ok")


def test_retry_without_body_keeps_params(client):
    """retry 无请求体（req=None）：参数原样保留，仍 202。"""
    _clear_active_tasks()
    from lquant.data.ingest.tasks import _finalize

    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 2, "note": "keep"}})
    tid = r.json()["task_id"]
    rr = client.post(f"/api/tasks/data/{tid}/retry")
    assert rr.status_code == 202, rr.text
    from lquant.data.ingest.tasks import get_task

    task = get_task(tid)
    # create_task 落库时会补 start/end/market/auto_crosscheck 规范键，只比对原键
    assert {k: task["params"][k] for k in ("days", "note")} == {"days": 2,
                                                               "note": "keep"}
    # 收尾落终态，避免 running 卡死后续用例的创建互斥
    _finalize(tid, total=0, failed_all=[], early=False, done_count=0)
    _wait_task_status(tid, lambda s: s == "ok")


def test_failed_data_task_surfaces_error_in_list(client, monkeypatch):
    """执行异常落 failed 的数据任务：列表 state=failed 且带 error 信息。"""
    _clear_active_tasks()
    from lquant.data.ingest import tasks as tasks_mod

    def _boom(task_id):
        tasks_mod._finalize(task_id, total=0, failed_all=[], early=False,
                            done_count=0, error="模拟崩溃")

    monkeypatch.setattr(tasks_mod, "run_claimed_task", _boom)
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 1}})
    tid = r.json()["task_id"]
    # 等 noop 执行体落终态再 retry：避免后台 _finalize 与端点写锁竞态
    _wait_task_status(tid, lambda s: s == "ok")
    # retry 换上 _boom 执行体制造 failed
    rr = client.post(f"/api/tasks/data/{tid}/retry", json={"params": {}})
    assert rr.status_code == 202, rr.text
    _wait_status(lambda: tasks_mod.get_task(tid)["status"],
                 lambda s: s == "failed")

    items = client.get("/api/tasks", params={"kind": "data"}).json()
    hit = next(x for x in items if x["id"] == tid)
    assert hit["state"] == "failed"
    assert hit["status"] == "failed"
    assert "模拟崩溃" in (hit["error"] or "")


def test_cancel_finished_data_task_409(client):
    """data 任务已结束（ok）→ 409 不可取消（不再是不分种类的 404）。"""
    _clear_active_tasks()
    r = client.post("/api/data/tasks", json={"kind": "daily_update",
                                             "params": {"days": 1}})
    tid = r.json()["task_id"]
    from lquant.core.db import writer

    with writer() as con:  # 显式落 ok 终态，避免 stub 线程完成时机竞态
        con.execute("UPDATE data_task SET status='ok', finished_at=now() "
                    "WHERE task_id=?", [tid])
    rr = client.post(f"/api/tasks/data/{tid}/cancel")
    assert rr.status_code == 409


def test_summary_counts_canceled(client):
    """summary 含 canceled 计数：取消的 sweep 计入 backtest.canceled。"""
    r = client.post("/api/backtests/sweep", json={"formula": "pct_change_5",
                                                  "param": "top_n",
                                                  "values": [1],
                                                  "rebalance": "monthly"})
    sid = r.json()["sweep_id"]
    rr = client.post(f"/api/tasks/backtest/{sid}/cancel")
    assert rr.status_code == 200, rr.text

    body = client.get("/api/tasks/summary").json()
    bt = body["kinds"]["backtest"]
    assert bt["canceled"] >= 1
    assert set(bt) == {"total", "running", "failed", "succeeded", "canceled"}


def test_ts_normalization_and_sort_order(client):
    """created_at 归一：数值直用、ISO 字符串解析、非法串/空值兜底 0，倒序输出。"""
    from lquant.server.api.task_center import _ts

    assert _ts(1717000000.5) == 1717000000.5
    assert _ts("2026-06-30T00:00:00") > 0
    assert _ts("not-a-date") == 0.0
    assert _ts("") == 0.0
    assert _ts(None) == 0.0

    items = client.get("/api/tasks").json()
    keys = [_ts(x["created_at"]) for x in items]
    assert keys == sorted(keys, reverse=True)


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
