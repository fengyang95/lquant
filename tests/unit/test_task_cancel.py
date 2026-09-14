"""data 任务协作式取消：backfill_pool 批间探针 → run_claimed_task 置 interrupted
→ 任务中心 cancel_ep kind=data。

分三层测：
1. backfill_pool 直接喂 fake provider + cancel_check → 批间提前停，canceled=True；
2. run_claimed_task(cancel_check=...) → data_task 终态 interrupted（可 retry 续传）；
3. HTTP cancel_ep：data 任务 200 / 已结束 409 / 不存在 404。
"""
from __future__ import annotations

import os
from datetime import date

import polars as pl
import pytest

from lquant.data.ingest.checkpoint import Checkpoint

# ---------- 1. backfill_pool 批间取消 ----------


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class CountingProvider:
    """每次 daily_bars 返回 1 行并计数 —— 判断取消发生在第几批之后。"""

    def __init__(self) -> None:
        self.calls = 0

    def daily_bars(self, symbols, start, end):
        self.calls += 1
        return pl.DataFrame({
            "symbol": list(symbols),
            "trade_date": [date(2024, 1, 2)] * len(symbols),
            "close": [1.0] * len(symbols),
        })


def _pool(n: int) -> list[tuple[str, date]]:
    return [(f"6001{i:02d}.SH", date(2024, 1, 2)) for i in range(n)]


def test_backfill_pool_cancel_before_first_batch(fake_settings):
    from lquant.data.ingest.daily import backfill_pool

    res = backfill_pool(_pool(6), date(2024, 1, 1), provider=CountingProvider(),
                        batch_size=2, cp_name="t-cancel-0",
                        cancel_check=lambda: True)
    assert res["canceled"] is True
    assert res["done"] == 0
    assert res["failed"] == []


def test_backfill_pool_cancel_between_batches(fake_settings):
    from lquant.data.ingest.daily import backfill_pool

    state = {"batches": 0}

    def cancel_after_first() -> bool:
        return state["batches"] >= 1

    class Probe(CountingProvider):
        def daily_bars(self, symbols, start, end):
            res = super().daily_bars(symbols, start, end)
            state["batches"] += 1
            return res

    probe = Probe()
    res = backfill_pool(_pool(6), date(2024, 1, 1), provider=probe,
                        batch_size=2, cp_name="t-cancel-1",
                        cancel_check=cancel_after_first)
    assert res["canceled"] is True
    assert probe.calls == 1  # 第一批跑完，第二批前停


def test_backfill_pool_no_cancel_runs_all(fake_settings):
    from lquant.data.ingest.daily import backfill_pool

    provider = CountingProvider()
    res = backfill_pool(_pool(4), date(2024, 1, 1), provider=provider,
                        batch_size=2, cp_name="t-cancel-2")
    assert res["canceled"] is False
    assert res["done"] == 4


# ---------- 2. run_claimed_task 取消 → interrupted ----------

@pytest.fixture
def task_env(fake_settings):
    """DDL + security 最小地基（_pool_from_con 要查 security）。"""

    from lquant.core.db import writer
    from lquant.data.ingest.tasks import _DDL
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        con.execute(_DDL)
        con.execute(
            "INSERT OR REPLACE INTO security (symbol, name, sec_type, board, "
            "list_date, is_st, source) VALUES "
            "('600100.SH', '假股', 'stock', 'main', DATE '2010-01-01', false, 'test'), "
            "('000200.SZ', '假股2', 'stock', 'main', DATE '2010-01-01', false, 'test')")
    yield


def _insert_running_task(task_id: str) -> None:
    import json
    from datetime import datetime

    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO data_task (task_id, kind, params, status, "
            "failed_symbols, failed_detail, started_at) "
            "VALUES (?, 'daily_update', ?, 'running', '[]'::JSON, '[]'::JSON, ?)",
            [task_id, json.dumps({"start": "2024-01-02", "end": "2024-01-10"}),
             datetime.now()],
        )


def test_run_claimed_task_canceled_interrupted(task_env, monkeypatch):
    """cancel_check 命中 → 终态 interrupted（不是 failed），可 retry 续传。"""

    from lquant.core.db import reader
    from lquant.data.ingest import tasks as tasks_mod

    captured: dict = {}

    def fake_backfill_pool(remaining, start, **kwargs):
        captured["cancel_check"] = kwargs.get("cancel_check")
        Checkpoint(tasks_mod._CP_PREFIX + "t-cancel").mark([s for s, _ in remaining])
        return {"done": len(remaining), "failed": [], "rows": 10,
                "early_stopped": False, "canceled": True}

    monkeypatch.setattr(tasks_mod, "backfill_pool", fake_backfill_pool)

    _insert_running_task("t-cancel")
    res = tasks_mod.run_claimed_task("t-cancel", cancel_check=lambda: True)
    assert captured["cancel_check"] is not None  # 探针确实被透传到池执行器
    assert res["status"] == "interrupted"
    assert res["message"] and "取消" in res["message"]

    with reader() as con:
        row = con.execute(
            "SELECT status, finished_at FROM data_task WHERE task_id='t-cancel'"
        ).fetchone()
    assert row[0] == "interrupted" and row[1] is not None
    # interrupted 在可 retry 集合里 —— 取消后能续传
    from lquant.data.ingest.tasks import _RETRIABLE

    assert "interrupted" in _RETRIABLE


def test_run_claimed_task_not_canceled_keeps_normal_flow(task_env, monkeypatch):
    """cancel_check 恒 False → 正常 ok 终态（回归保护）。"""
    from lquant.data.ingest import tasks as tasks_mod

    def fake_backfill_pool(remaining, start, **kwargs):
        Checkpoint(tasks_mod._CP_PREFIX + "t-ok").mark([s for s, _ in remaining])
        return {"done": len(remaining), "failed": [], "rows": 5,
                "early_stopped": False, "canceled": False}

    monkeypatch.setattr(tasks_mod, "backfill_pool", fake_backfill_pool)
    monkeypatch.setattr(tasks_mod, "_auto_crosscheck", lambda *a, **k: None)

    _insert_running_task("t-ok")
    res = tasks_mod.run_claimed_task("t-ok", cancel_check=lambda: False)
    assert res["status"] == "ok"


def test_mark_canceled_pending_only_touches_pending(task_env):
    """排队取消只动 pending：running/ok 不越权，不存在返回 False。"""
    from lquant.data.ingest import tasks as tasks_mod

    assert tasks_mod.mark_canceled_pending("t-nonexistent") is False

    _insert_running_task("t-run")
    assert tasks_mod.mark_canceled_pending("t-run") is False  # running 不动

    import json
    from datetime import datetime

    from lquant.core.db import writer

    with writer() as con:  # 造一个 pending
        con.execute(
            "INSERT OR REPLACE INTO data_task (task_id, kind, params, status, "
            "started_at) VALUES (?, 'daily_update', ?::JSON, 'pending', ?)",
            ["t-pend", json.dumps({"start": "2024-01-02", "end": "2024-01-10"}),
             datetime.now()],
        )
    assert tasks_mod.mark_canceled_pending("t-pend") is True
    from lquant.core.db import reader

    with reader() as con:
        row = con.execute(
            "SELECT status, message FROM data_task WHERE task_id='t-pend'"
        ).fetchone()
    assert row[0] == "interrupted" and "排队中" in row[1]


def test_mark_canceled_pending_idempotent(task_env):
    """重复排队取消幂等：第二次返回 False，状态不回退。"""
    import json
    from datetime import datetime

    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO data_task (task_id, kind, params, status, "
            "started_at) VALUES (?, 'daily_update', ?::JSON, 'pending', ?)",
            ["t-pend2", json.dumps({"start": "2024-01-02"}), datetime.now()],
        )
    from lquant.data.ingest import tasks as tasks_mod

    assert tasks_mod.mark_canceled_pending("t-pend2") is True
    assert tasks_mod.mark_canceled_pending("t-pend2") is False
    from lquant.core.db import reader

    with reader() as con:
        assert con.execute(
            "SELECT status FROM data_task WHERE task_id='t-pend2'"
        ).fetchone()[0] == "interrupted"


# ---------- 3. HTTP cancel_ep（kind=data） ----------

os.environ.setdefault("LQ_SYNC_WORKER", "0")


@pytest.mark.usefixtures("api_env_http")
class TestCancelEndpoint:
    @pytest.fixture(autouse=True)
    def _stub_executor(self, monkeypatch):
        """执行器 no-op：不让 demo 环境真去拉网络日线。"""

        def _noop(task_id):
            from lquant.data.ingest.tasks import get_task

            return get_task(task_id)

        from lquant.data.ingest import tasks as tasks_mod
        from lquant.server.api import data as data_mod

        monkeypatch.setattr(tasks_mod, "execute_task", _noop)
        monkeypatch.setattr(tasks_mod, "run_claimed_task", _noop)
        monkeypatch.setattr(data_mod, "execute_task", _noop)
        monkeypatch.setattr(data_mod, "run_claimed_task", _noop)
        from lquant.core.db import writer

        with writer() as con:
            con.execute(tasks_mod._DDL)
            con.execute("DELETE FROM data_task")
        yield

    def test_cancel_data_task_pending(self, client):
        # 创建任务（执行器已打桩 no-op）→ pending，可取消
        r = client.post("/api/data/tasks",
                        json={"kind": "daily_update", "params": {"days": 3}})
        assert r.status_code == 202
        task_id = r.json()["task_id"]
        r = client.post(f"/api/tasks/data/{task_id}/cancel")
        assert r.status_code == 200
        assert r.json()["canceled"] is True

    def test_cancel_finished_task_409(self, client):
        r = client.post("/api/data/tasks",
                        json={"kind": "daily_update", "params": {"days": 3}})
        task_id = r.json()["task_id"]
        # 手动置 ok（已结束）→ 409
        from lquant.core.db import writer

        with writer() as con:
            con.execute(
                "UPDATE data_task SET status='ok', finished_at=now() "
                "WHERE task_id=?", [task_id])
        r = client.post(f"/api/tasks/data/{task_id}/cancel")
        assert r.status_code == 409

    def test_cancel_unknown_task_404(self, client):
        r = client.post("/api/tasks/data/nonexistent/cancel")
        assert r.status_code == 404

    def test_cancel_unknown_kind_422(self, client):
        r = client.post("/api/tasks/sync/nonexistent/cancel")
        assert r.status_code in (404, 422)


@pytest.fixture(scope="module")
def api_env_http(tmp_path_factory):
    base = tmp_path_factory.mktemp("task_cancel_http")
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
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env_http):
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c
