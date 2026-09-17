"""API 层：/api/data/gaps 缺口查询 + /api/data/gaps/repair 一键补采。

自建环境（不依赖 demo 数据）：交易日历工作日全开市 + 3 只标的 +
日线湖故意缺一天，验证缺口读取与补采任务创建的 HTTP 契约。
后台执行器打桩为 no-op，队列行为在 ingest tasks 测试覆盖。
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("gaps_env")


@pytest.fixture(scope="module")
def gaps_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_gaps")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)

    # 交易日历：2026-09-07(周一) ~ 2026-09-11(周五) 工作日全开市
    start, end = date(2026, 9, 7), date(2026, 9, 11)
    d, rows = start, []
    while d <= end:
        if d.weekday() < 5:
            rows.append(d)
        d += timedelta(days=1)
    cal = pl.DataFrame(
        {"trade_date": rows, "is_open": [True] * len(rows)},
        schema_overrides={"trade_date": pl.Date},
    )
    syms = ["600000.SH", "000001.SZ", "510300.SH"]
    with writer() as con:
        con.register("_cal", cal)
        con.execute("INSERT INTO trade_calendar "
                    "SELECT trade_date, is_open, 'test' FROM _cal")
        for s in syms:
            con.execute(
                "INSERT OR REPLACE INTO security (symbol, sec_type, list_date) "
                "VALUES (?, 'stock', ?)", [s, date(2000, 1, 1)])

    # 日线湖：只写前 4 天 —— 最后一个交易日 09-11 整日缺失
    n = len(syms) * len(rows[:4])
    df = pl.DataFrame({
        "symbol": syms * 4,
        "trade_date": rows[:4] * len(syms),
        "close": [1.0] * n,
    }, schema_overrides={"trade_date": pl.Date})
    from lquant.data.store.parquet import write_daily

    write_daily(df)
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(gaps_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_executor(monkeypatch):
    """后台执行器 no-op，且每次用例后清残留 pending 任务（create 互斥）。"""

    def _noop(task_id):
        from lquant.data.ingest.tasks import get_task

        return get_task(task_id)

    monkeypatch.setattr(
        "lquant.data.ingest.tasks.execute_task", _noop, raising=False)
    yield
    from lquant.core.db import writer

    try:
        with writer() as con:
            con.execute(
                "DELETE FROM data_task WHERE status IN ('pending', 'running')")
    except Exception:  # noqa: BLE001 - data_task 表未建过时无需清理
        pass


def test_gaps_reports_missing_day(client) -> None:
    r = client.get("/api/data/gaps")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] and body["window"]["end"]
    daily = next(t for t in body["datasets"] if t["dataset"] == "daily")
    assert daily["expected_days"] == 5
    assert daily["actual_days"] == 4
    assert daily["missing"] == ["2026-09-11"]
    # 整日缺失按 per-symbol 稀疏口径也会报（3 只标的当天都没数据）
    assert daily["sparse_total"] == 3


def test_gaps_repair_creates_daily_update(client, monkeypatch) -> None:
    from lquant.server.api import data as data_api

    enqueued: list[tuple] = []
    monkeypatch.setattr(
        data_api, "enqueue",
        lambda queue, fn, *args, **kw: enqueued.append((queue, fn.__name__, args)))
    r = client.post("/api/data/gaps/repair")
    assert r.status_code == 202
    body = r.json()
    assert body["created"] is True
    task_id = body["task_id"]
    # 必须入队执行器，否则任务永远 pending（PR#66 的回归点）。
    # fn 是 _stub_executor 打桩后的 tasks.execute_task，名字不关键，
    # 关键是：queue 正确 + 以 task_id 为 job_id 入队（cancel 探针依赖）
    assert enqueued and enqueued[0][0] == "lquant-ingest"
    assert enqueued[0][2][0] == task_id
    task = client.get(f"/api/data/tasks/{task_id}").json()
    assert task["kind"] == "daily_update"
    params = task["params"]
    assert params["start"] == "2026-09-11"
    assert params["end"] >= "2026-09-11"


def test_gaps_repair_conflict_returns_not_created(client) -> None:
    from lquant.data.ingest import tasks as t

    # 占一个 pending 任务 → repair 应返回 created=False 而非 500
    t.create_task("daily_update", {"days": 1})
    r = client.post("/api/data/gaps/repair")
    assert r.status_code == 202
    assert r.json()["created"] is False
    assert r.json()["reason"] == "active_task_exists"


def test_gaps_repair_no_gap(client, monkeypatch) -> None:
    """无缺口分支：missing 为空时 created=False, reason=no_gap（打桩报告）。"""
    from lquant.server.api import data as data_api

    monkeypatch.setattr(
        data_api, "_gaps_report",
        lambda days=30: {
            "window": {"start": "2026-08-01", "end": "2026-09-16"},
            "datasets": [{"dataset": "daily", "label": "日线湖",
                          "expected_days": 5, "actual_days": 5,
                          "missing": [], "sparse_symbols": {}, "sparse_total": 0}],
            "missing_dates": [],
        })
    r = client.post("/api/data/gaps/repair")
    assert r.status_code == 202
    assert r.json() == {"created": False, "task_id": None, "reason": "no_gap"}
