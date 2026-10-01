"""因子评价任务化：202 入队 → 进度流 → 结果落库 → 任务中心可见。"""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from lquant.server import jobs as jobs_mod


@pytest.fixture(scope="module", autouse=True)
def _factor_eval_env(tmp_path_factory):
    """自包含 demo 环境：因子评价需要非空日线湖。

    此前依赖开发者本地 data/ 湖（未入库）—— CI 与全新 worktree 上必失败。
    """
    base = tmp_path_factory.mktemp("factor_eval_task")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch: pytest.MonkeyPatch):
    """强制本地降级队列（测试环境不依赖 Redis/RQ worker）。"""
    monkeypatch.setattr(jobs_mod, "_probe_redis", lambda *a, **k: False)
    jobs_mod._redis_probe_ok = None
    jobs_mod._redis_probe_at = 0.0
    yield
    jobs_mod._redis_probe_ok = None
    jobs_mod._redis_probe_at = 0.0


def _client() -> TestClient:
    from lquant.server.main import create_app

    return TestClient(create_app())


def _wait_finished(job_id: str, timeout: float = 180.0) -> str:
    from lquant.server.jobs import get_job

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job is None:
            break
        st = job.get_status()
        if st in ("finished", "failed", "canceled"):
            return st
        time.sleep(0.1)
    return "timeout"


def _payload(factor: str, formula: str) -> dict:
    return {"factor": factor, "formula": formula,
            "start": "2024-01-02", "end": "2024-02-01", "n_groups": 3}


def test_evaluate_task_full_flow() -> None:
    """202 入队 → 任务体跑完 → 进度阶段齐全 → 结果落库可查。"""
    c = _client()
    factor = f"evalflow{int(time.time())}"
    r = c.post("/api/factors/evaluate", json=_payload(factor, "pct_change_5"))
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    assert jid == f"factor-eval-{factor}"  # 确定性 id：同因子重跑覆盖

    assert _wait_finished(jid) == "finished"

    # 进度阶段齐全（任务已跑完，注册表仍保留终值）
    from lquant.server.progress import get_progress

    p = get_progress(jid)
    assert p is not None and p["total"] == 100
    assert p["done"] >= 95  # 最后一步「汇总」= 95

    # 结果落库：REST 查询端点可取回（报告 URL 在内）
    r2 = c.get(f"/api/factors/evaluate/{jid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["result"]["factor"] == factor
    assert body["result"]["report_url"].startswith("/api/factors/reports/")


def test_task_center_lists_eval_task() -> None:
    """评价任务出现在任务中心 factor 类别，名称「因子评价」。"""
    c = _client()
    factor = f"tc{int(time.time())}"
    r = c.post("/api/factors/evaluate", json=_payload(factor, "turnover"))
    assert r.status_code == 202
    jid = r.json()["job_id"]
    assert _wait_finished(jid) == "finished"

    items = c.get("/api/tasks?kind=factor&limit=100").json()
    mine = [t for t in items if t["id"] == jid]
    assert mine, f"任务中心缺 {jid}"
    assert mine[0]["name"] == "因子评价"


def test_ws_streams_progress_then_result() -> None:
    """WS /ws/jobs/{id}：进度随流下发，终态带 result。"""
    c = _client()
    factor = f"ws{int(time.time())}"
    r = c.post("/api/factors/evaluate", json=_payload(factor, "rolling_std_10"))
    jid = r.json()["job_id"]
    with c.websocket_connect(f"/ws/jobs/{jid}") as ws:
        saw_progress = False
        result = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("progress") is not None:
                saw_progress = True
            if msg.get("done"):
                result = msg.get("result")
                break
        assert saw_progress, "WS 流中未见到 progress 字段"
        assert result is not None and result["factor"] == factor


def test_enqueue_injects_progress_callback() -> None:
    """fn 声明 progress 形参时自动注入回调，进度写入注册表。"""

    def job_fn(progress=None) -> None:
        progress(done=1, total=4, phase="x")

    job = jobs_mod.enqueue("lquant-mining", job_fn)
    from lquant.server.progress import get_progress

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        p = get_progress(job.id)
        if p is not None:
            break
        time.sleep(0.05)
    assert job.get_status() == "finished"
    assert get_progress(job.id)["total"] == 4
