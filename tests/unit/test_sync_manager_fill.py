"""sync/manager.py 覆盖补齐：各作业类型执行分支、异常降级、调度循环。

全部用 monkeypatch 打桩外部采集/回填函数，离线且不依赖真实湖。
"""
from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("LQ_SYNC_WORKER", "0")


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """隔离 LQ_ROOT：独立 duckdb（空库，manager 自建表）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------- run_job 各类型

def test_crud_roundtrip(sync_env) -> None:
    """seed/upsert/list/set_enabled/delete 全链路（空库自建表）。"""
    from lquant.sync import manager

    manager.seed_defaults()
    assert manager.seed_defaults() == 0  # 幂等
    jobs = {j["sync_id"]: j for j in manager.list_jobs()}
    assert {"close", "daily", "adj"} <= set(jobs)

    manager.upsert_job("t1", "测试", "collect", "16:00", weekdays="5",
                       params={"schedule": "close"})
    manager.upsert_job("t1", "测试改", "collect", "17:00")  # 覆盖且保留轨迹
    j = {x["sync_id"]: x for x in manager.list_jobs()}["t1"]
    assert j["schedule_time"] == "17:00"

    manager.set_enabled("t1", False)
    assert {x["sync_id"]: x for x in manager.list_jobs()}["t1"]["enabled"] is False
    manager.delete_job("t1")
    assert "t1" not in {x["sync_id"]: x for x in manager.list_jobs()}


def test_upsert_preserves_run_state(sync_env) -> None:
    """编辑作业（调度时间不变）不清空 last_run_at/status；
    调度时间变化则重置轨迹（旧轨迹对应旧调度）。"""
    from lquant.core.db import writer
    from lquant.sync import manager

    manager.upsert_job("keep", "k", "collect", "16:00")
    now = datetime.now()
    with writer() as con:
        manager._ensure_tables(con)
        con.execute(
            "UPDATE sync_job SET last_run_at = ?, last_status = 'ok', last_rows = 3 "
            "WHERE sync_id = 'keep'", [now])
    manager.upsert_job("keep", "k", "collect", "16:00")
    j = {x["sync_id"]: x for x in manager.list_jobs()}["keep"]
    assert j["last_status"] == "ok" and j["last_rows"] == 3
    assert j["last_run_at"] is not None
    # 时间变了 → 轨迹重置
    manager.upsert_job("keep", "k", "collect", "17:00")
    j2 = {x["sync_id"]: x for x in manager.list_jobs()}["keep"]
    assert j2["last_status"] is None and j2["last_run_at"] is None


def test_upsert_rejects_bad_daily_market(sync_env) -> None:
    from lquant.sync import manager

    with pytest.raises(ValueError):
        manager.upsert_job("bad", "b", "daily", "10:00", params={"market": "weird"})


def test_run_job_unknown_kind(sync_env, monkeypatch) -> None:
    res = _run(_job("nope"), monkeypatch)
    assert res["status"] == "failed"
    assert "未知作业类型" in res["detail"]["error"]


def test_trading_day_ok_calendar_paths(sync_env) -> None:
    from lquant.core.db import writer
    from lquant.sync import manager

    with writer() as con:
        con.execute("CREATE TABLE IF NOT EXISTS trade_calendar ("
                    "trade_date DATE PRIMARY KEY, is_open BOOLEAN, source VARCHAR)")
        con.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?, true, 't')",
                    [datetime(2026, 9, 8).date()])
        con.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?, false, 't')",
                    [datetime(2026, 9, 9).date()])
    assert manager._trading_day_ok(datetime(2026, 9, 8).date()) is True
    assert manager._trading_day_ok(datetime(2026, 9, 9).date()) is False
    assert manager._trading_day_ok(datetime(2027, 1, 1).date()) is True  # 缺失 → 放行


def _job(kind, params=None, sync_id="probe"):
    return {"sync_id": sync_id, "name": "探针", "kind": kind,
            "params": params or {}}


def test_run_job_daily_sentinel(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.daily as daily_mod

    monkeypatch.setattr(daily_mod, "backfill_daily",
                        lambda full, start: 42, raising=False)
    res = _run(_job("daily", {"market": "sentinel", "days": 5}), monkeypatch)
    assert res["status"] == "ok"
    assert res["rows"] == 42


def _run(job, monkeypatch, demo=None, **patches):
    from lquant.sync import manager

    for name, fn in patches.items():
        mod_name, attr = name.rsplit(".", 1)
        monkeypatch.setattr(mod_name, attr, fn)
    if demo is None:
        return manager.run_job(job)
    return manager.run_job(job, demo=demo)


def test_run_job_daily_all(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "create_task",
                        lambda kind, params: {"task_id": "t1"})
    monkeypatch.setattr(tasks_mod, "execute_task",
                        lambda tid: {"status": "ok", "rows_written": 7,
                                     "message": "done"})
    res = _run(_job("daily", {"days": 3, "auto_crosscheck": False}), monkeypatch)
    assert res["status"] == "ok" and res["rows"] == 7
    assert res["detail"]["task_id"] == "t1"


def test_run_job_daily_conflict_skipped(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.tasks as tasks_mod

    def _conflict(kind, params):
        raise tasks_mod.TaskConflictError("已有任务在跑")

    monkeypatch.setattr(tasks_mod, "create_task", _conflict)
    res = _run(_job("daily", {"days": 3}), monkeypatch)
    assert res["status"] == "skipped"
    assert res["detail"]["skipped"] is True
    assert res["rows"] == 0  # skipped 不再判 zero_rows partial


def test_run_job_daily_partial_and_failed(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "create_task",
                        lambda kind, params: {"task_id": "t1"})
    monkeypatch.setattr(tasks_mod, "execute_task",
                        lambda tid: {"status": "partial", "rows_written": 1,
                                     "message": None})
    res = _run(_job("daily", {}), monkeypatch)
    assert res["status"] == "partial"

    monkeypatch.setattr(tasks_mod, "execute_task",
                        lambda tid: {"status": "failed", "rows_written": 0,
                                     "message": "网络挂"})
    res = _run(_job("daily", {}), monkeypatch)
    assert res["status"] == "failed"


def test_run_job_daily_bad_market(monkeypatch, sync_env) -> None:
    res = _run(_job("daily", {"market": "weird"}), monkeypatch)
    assert res["status"] == "failed"
    assert "all/sentinel" in res["detail"]["error"]


def test_run_job_adj_factor(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.adj as adj_mod

    monkeypatch.setattr(adj_mod, "refresh_adj_factors", lambda days: 9,
                        raising=False)
    res = _run(_job("adj_factor", {"days": 60}), monkeypatch)
    assert res["rows"] == 9 and res["status"] == "ok"


def test_run_job_reference_dict_and_other(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.reference as ref_mod

    monkeypatch.setattr(ref_mod, "sync_reference",
                        lambda skip_details: {"calendar": 3, "securities": 4},
                        raising=False)
    res = _run(_job("reference", {}), monkeypatch)
    assert res["rows"] == 7

    monkeypatch.setattr(ref_mod, "sync_reference",
                        lambda skip_details: "synced-ok", raising=False)
    res = _run(_job("reference", {"skip_details": True}), monkeypatch)
    assert res["status"] == "ok" and res["detail"]["result"] == "synced-ok"


def test_run_job_daily_basic(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.daily_basic as db_mod

    monkeypatch.setattr(db_mod, "backfill_daily_basic",
                        lambda start, merge: {"rows": 11}, raising=False)
    res = _run(_job("daily_basic", {"days": 14, "merge": True}), monkeypatch)
    assert res["rows"] == 11


def test_run_job_financial(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.financial as fin_mod
    import lquant.data.store.catalog as cat_mod

    monkeypatch.setattr(cat_mod, "SecurityRepo",
                        lambda: SimpleNamespace(stock_symbols=lambda include_delisted: ["600519.SH"]),
                        raising=False)
    monkeypatch.setattr(fin_mod, "backfill_financial",
                        lambda symbols, start: 21, raising=False)
    res = _run(_job("financial", {"days": 90}), monkeypatch)
    assert res["rows"] == 21 and res["detail"]["symbols"] == 1


def test_run_job_backfill(monkeypatch, sync_env) -> None:
    import lquant.data.quality.coverage as cov_mod
    import lquant.market.backfill as bf_mod

    monkeypatch.setattr(bf_mod, "ensure_market_coverage",
                        lambda days: {"persisted": 5}, raising=False)
    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair: {"repair": {"created": 0}},
                        raising=False)
    res = _run(_job("backfill", {"days": 30}), monkeypatch)
    assert res["rows"] == 5 and res["status"] == "ok"

    # 修复建了补采任务 → partial
    monkeypatch.setattr(cov_mod, "scan_coverage",
                        lambda days, repair: {"repair": {"created": 2}},
                        raising=False)
    res = _run(_job("backfill", {}), monkeypatch)
    assert res["status"] == "partial"


def test_run_job_exception_recorded(monkeypatch, sync_env) -> None:
    import lquant.data.ingest.adj as adj_mod

    def boom(days):
        raise RuntimeError("源炸了")

    monkeypatch.setattr(adj_mod, "refresh_adj_factors", boom, raising=False)
    res = _run(_job("adj_factor", {}), monkeypatch)
    assert res["status"] == "failed"
    assert "RuntimeError" in res["detail"]["error"]


def test_run_job_zero_rows_partial(monkeypatch, sync_env) -> None:
    """0 行不算健康：daily/collect/adj_factor 空返回记 partial。"""
    import lquant.data.ingest.adj as adj_mod

    monkeypatch.setattr(adj_mod, "refresh_adj_factors", lambda days: 0,
                        raising=False)
    res = _run(_job("adj_factor", {}), monkeypatch)
    assert res["status"] == "partial" and res["detail"]["zero_rows"] is True


def test_run_job_collect(monkeypatch, sync_env) -> None:
    import lquant.market.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "collect_and_save",
                        lambda schedule, trade_date, demo: {
                            "persisted": {"limit_up": 3},
                            "collected": {"limit_up": 3},
                            "errors": {"index_daily": "x"}},
                        raising=False)
    res = _run(_job("collect", {"schedule": "close", "demo": True}), monkeypatch)
    assert res["status"] == "partial" and res["rows"] == 3


def test_run_job_collect_demo_flag(monkeypatch, sync_env) -> None:
    """demo=None → 取 params.demo；显式 demo 参数覆盖。"""
    import lquant.market.scheduler as sched_mod

    seen: dict = {}

    def fake(schedule, trade_date, demo):
        seen["demo"] = demo
        return {"persisted": {"x": 1}, "collected": {}, "errors": {}}

    monkeypatch.setattr(sched_mod, "collect_and_save", fake, raising=False)
    res = _run(_job("collect", {"demo": True}), monkeypatch)
    assert seen["demo"] is True and res["status"] == "ok"
    res = _run(_job("collect", {}), monkeypatch, demo=False)
    assert seen["demo"] is False


def test_run_job_sync_run_record_failure_swallowed(monkeypatch, sync_env) -> None:
    """sync_run 落库失败只 log，不炸作业。"""
    import lquant.data.ingest.adj as adj_mod
    from lquant.sync import manager

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db lock")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(adj_mod, "refresh_adj_factors", lambda days: 3,
                        raising=False)
    monkeypatch.setattr(manager, "writer", lambda: _Boom())
    res = manager.run_job(_job("adj_factor", {}))
    assert res["status"] == "ok"  # 执行成功，仅记录失败被吞


def test_history_reads_sync_run(sync_env, monkeypatch) -> None:
    import lquant.data.ingest.adj as adj_mod
    from lquant.sync import manager

    monkeypatch.setattr(adj_mod, "refresh_adj_factors", lambda days: 2,
                        raising=False)
    res = manager.run_job(_job("adj_factor", {}))
    hist = manager.history(limit=5)
    assert any(h["run_id"] == res["run_id"] for h in hist)
    assert hist[0]["status"] in ("ok", "partial", "failed")


def test_run_job_emit_error_to_ring(monkeypatch, sync_env) -> None:
    """终态非 ok/skipped → monitor 错误环收到 ApiErrorPoint。"""
    import lquant.data.ingest.adj as adj_mod

    points: list = []

    class FakeRing:
        def append(self, p):
            points.append(p)

    monkeypatch.setattr("lquant.monitor.ring.error_ring", FakeRing())
    monkeypatch.setattr(adj_mod, "refresh_adj_factors",
                        lambda days: (_ for _ in ()).throw(RuntimeError("x")),
                        raising=False)
    res = _run(_job("adj_factor", {"days": 5}), monkeypatch)
    assert res["status"] == "failed"
    assert points and points[0].route == "sync://probe"
    assert points[0].status == 500


# ---------------------------------------------------------------- 调度判定

def test_is_due_edge_cases() -> None:
    from lquant.sync.manager import _is_due

    base = {"sync_id": "t", "kind": "collect", "schedule_time": "15:05",
            "weekdays": "1,2,3,4,5", "enabled": True, "last_run_at": None}
    # weekdays 带空格也认
    monday = datetime(2026, 9, 7, 16, 0)
    assert _is_due({**base, "weekdays": " 1, 3 "}, monday) is True
    # 非调度日
    assert _is_due({**base, "weekdays": " 1, 3 "}, datetime(2026, 9, 8, 16, 0)) is False
    # 坏 schedule_time（string compare 蒙混过关但 split 失败）→ False
    assert _is_due({**base, "schedule_time": "zz:zz"}, monday) is False
    # last_run_at 为 ISO 字符串（DB 读回形态）
    assert _is_due({**base, "last_run_at": "2026-09-01 10:00:00"}, monday) is True
    assert _is_due({**base, "last_run_at": "2026-09-07 16:00:00"}, monday) is False


def test_trading_day_ok_fallbacks(monkeypatch) -> None:
    from lquant.sync import manager

    # reader 不可用 → 放行
    class _Boom:
        def __enter__(self):
            raise RuntimeError("down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(manager, "reader", lambda: _Boom())
    assert manager._trading_day_ok(datetime(2026, 9, 8).date()) is True


def test_tick_no_trading_day(monkeypatch) -> None:
    from lquant.sync import manager

    monkeypatch.setattr(manager, "_trading_day_ok", lambda d: False)
    assert manager.tick() == []


def test_tick_runs_due(monkeypatch) -> None:
    from lquant.sync import manager

    monkeypatch.setattr(manager, "_trading_day_ok", lambda d: True)
    jobs = [{"sync_id": "a", "name": "a", "kind": "collect",
             "schedule_time": "00:00", "weekdays": "1,2,3,4,5,6,7",
             "params": {}, "enabled": True, "last_run_at": None},
            {"sync_id": "b", "name": "b", "kind": "collect",
             "schedule_time": "23:59", "weekdays": "1,2,3,4,5,6,7",
             "params": {}, "enabled": False, "last_run_at": None}]
    monkeypatch.setattr(manager, "list_jobs", lambda: jobs)
    monkeypatch.setattr(manager, "run_job", lambda job: {"status": "ok", "rows": 1})
    now = datetime(2026, 9, 7, 12, 0)
    out = manager.tick(now=now)
    assert [r["sync_id"] for r in out] == ["a"]
    assert out[0]["status"] == "ok"


def test_loop_forever_logs_and_survives(monkeypatch) -> None:
    """tick 结果逐条 log；tick 抛异常不退出循环；KeyboardInterrupt 可终止。"""
    import time as time_mod

    from loguru import logger

    from lquant.sync import manager

    calls = {"n": 0}

    def fake_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"sync_id": "a", "status": "ok", "rows": 3}]
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "tick", fake_tick)
    sleeps: list = []
    monkeypatch.setattr(time_mod, "sleep", lambda s: sleeps.append(s))
    records: list = []
    hid = logger.add(records.append, level="DEBUG")
    with pytest.raises(KeyboardInterrupt):
        manager.loop_forever(interval=30)
    logger.remove(hid)
    assert calls["n"] == 2
    assert sleeps == [30]
    assert any("sync] a" in str(m) for m in records)
