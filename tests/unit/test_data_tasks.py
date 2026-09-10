"""data_task 表 + 执行器单元测试（不联网，mock backfill_pool）。

隔离方式沿用 T3 报告结论：LQ_ROOT env + chdir + cache_clear，
不 patch get_settings 模块属性（会污染懒加载绑定）。
"""
from __future__ import annotations

from datetime import date

import pytest

from lquant.data.ingest import tasks as tasks_mod
from lquant.data.ingest.checkpoint import Checkpoint

# ---------------------------------------------------------------- fixtures


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """LQ_ROOT + chdir + cache_clear 隔离（不 patch get_settings 模块属性）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def seeded_db(fake_settings):
    """临时 DuckDB：security 表 2 只在市 + 1 只退市 + 1 只 ETF。"""
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE security ("
            "symbol VARCHAR, sec_type VARCHAR, list_date DATE, delist_date DATE)"
        )
        con.execute(
            "INSERT INTO security VALUES "
            "('000001.SZ','stock',DATE '2000-01-01',NULL),"
            "('000002.SZ','stock',DATE '2000-01-01',NULL),"
            "('000003.SZ','stock',DATE '2000-01-01',DATE '2020-06-30'),"
            "('510300.SH','etf',DATE '2012-01-01',NULL)"
        )
    yield


def _mk_task(kind="full_backfill", **params):
    base = {"start": "2016-01-01", "end": "2024-12-31"}
    base.update(params)
    return tasks_mod.create_task(kind, base)


def _mk_running_row():
    from lquant.core.db import writer

    with writer() as con:
        con.execute(tasks_mod._DDL)
        con.execute(
            "INSERT INTO data_task (task_id, kind, params, status) VALUES "
            "('zzrunning01', 'full_backfill', ?::JSON, 'running')",
            ["{}"],
        )


def _cp(task_id: str) -> Checkpoint:
    return Checkpoint(f"daily:{task_id}")


# ---------------------------------------------------------------- mock


def _mock_backfill(calls, failed=None, rows=0, early=False):
    """tasks.backfill_pool 替身：记账 + 可选失败/早停。"""

    def fn(pool, start, end=None, on_progress=None, **kw):
        calls.append({"pool": list(pool), "start": start, "end": end})
        pool_syms = {s for s, _ in pool}
        script = [f for f in (failed or []) if f["symbol"] in pool_syms]
        failed_syms = {f["symbol"] for f in script}
        done = len(pool) - len(failed_syms)
        if on_progress:
            frame = {
                "done": done,
                "total": len(pool),
                "failed": list(script),
                "rows": rows,
                "early_stopped": False,
            }
            on_progress(frame)
        return {"done": done, "failed": list(script), "rows": rows,
                "early_stopped": early}

    return fn


# ---------------------------------------------------------------- 前置校验


def test_create_full_backfill_requires_delisted(seeded_db):
    from lquant.core.db import writer

    with writer() as con:
        con.execute("DELETE FROM security WHERE delist_date IS NOT NULL")
    with pytest.raises(ValueError, match="lq data reference"):
        tasks_mod.create_task("full_backfill", {"start": "2016-01-01"})


def test_create_rejects_running_task(seeded_db):
    _mk_running_row()
    with pytest.raises(ValueError, match="未完成"):
        tasks_mod.create_task("daily_update", {"days": 10})


def test_create_rejects_unknown_kind(seeded_db):
    with pytest.raises(ValueError, match="任务类型"):
        tasks_mod.create_task("nope", {})


def test_create_bad_start_format(seeded_db):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tasks_mod.create_task("daily_update", {"days": 10, "start": "2024/01/01"})


def test_create_full_backfill_ok(seeded_db):
    t = _mk_task()
    assert t["status"] == "pending"
    assert t["total_symbols"] == 4
    assert t["params"]["start"] == "2016-01-01"
    assert t["params"]["end"] == "2024-12-31"
    assert t["params"]["auto_crosscheck"] is True
    assert t["failed_symbols"] == []


def test_create_daily_update_params(seeded_db):
    t = tasks_mod.create_task("daily_update", {"days": 3})
    assert t["params"]["days"] == 3
    assert t["params"]["start"]
    assert t["params"]["end"]


def test_create_empty_pool_rejected(seeded_db):
    from lquant.core.db import writer

    with writer() as con:
        con.execute("DELETE FROM security")
    with pytest.raises(ValueError, match="回填池为空"):
        tasks_mod.create_task("daily_update", {"days": 10})


# ---------------------------------------------------------------- 池构建


def test_pool_full_includes_delisted_with_truncated_end(seeded_db):
    pool = tasks_mod._pool_with_ends(
        "full_backfill", date(2016, 1, 1), date(2024, 12, 31)
    )
    m = dict(pool)
    assert set(m) == {"000001.SZ", "000002.SZ", "000003.SZ", "510300.SH"}
    assert m["000003.SZ"] == date(2020, 6, 30)  # 退市截断
    assert m["000001.SZ"] == date(2024, 12, 31)
    assert m["510300.SH"] == date(2024, 12, 31)


def test_pool_daily_excludes_delisted(seeded_db):
    pool = tasks_mod._pool_with_ends(
        "daily_update", date(2024, 12, 1), date(2024, 12, 31)
    )
    m = dict(pool)
    assert set(m) == {"000001.SZ", "000002.SZ", "510300.SH"}
    assert all(e == date(2024, 12, 31) for e in m.values())


def test_pool_full_truncates_end_before_start(seeded_db):
    # 退市早于 start 也保留（T3 语义：空数据算 done），end=delist
    pool = tasks_mod._pool_with_ends(
        "full_backfill", date(2021, 1, 1), date(2024, 12, 31)
    )
    assert dict(pool)["000003.SZ"] == date(2020, 6, 30)


# ---------------------------------------------------------------- 执行/状态机


def test_execute_ok(seeded_db, monkeypatch):
    calls = []
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill(calls, rows=7))
    t = _mk_task()
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "ok"
    assert out["done_symbols"] == 4
    assert out["rows_written"] == 14  # 两 phase 各报 7 行
    assert out["finished_at"] is not None
    assert set(_cp(t["task_id"]).done) == {
        "000001.SZ", "000002.SZ", "000003.SZ", "510300.SH"
    }


def test_execute_partial(seeded_db, monkeypatch):
    calls = []
    failed = [{"symbol": "000001.SZ", "reason": "boom"}]
    monkeypatch.setattr(
        tasks_mod, "backfill_pool", _mock_backfill(calls, failed=failed, rows=5)
    )
    t = _mk_task()
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "partial"
    assert out["done_symbols"] == 3
    assert out["rows_written"] == 10  # 两 phase 各报 5 行
    assert out["failed_symbols"] == ["000001.SZ"]
    assert out["failed_detail"] == [{"symbol": "000001.SZ", "reason": "boom"}]
    cp = _cp(t["task_id"])
    assert "000002.SZ" in cp.done
    assert "000001.SZ" not in cp.done
    assert len(calls) == 2  # stocks + etf 两个 phase


def test_execute_early_stop_failed(seeded_db, monkeypatch):
    calls = []
    all_failed = [{"symbol": s, "reason": "down"} for s in
                  ("000001.SZ", "000002.SZ", "000003.SZ")]
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill(
        calls, failed=all_failed, early=True))
    t = _mk_task()
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "failed"
    assert out["phase"] == "stocks"  # 早停于 stocks phase，未进 ETF
    assert len(calls) == 1
    assert out["done_symbols"] == 0


def test_execute_all_failed(seeded_db, monkeypatch):
    all_failed = [{"symbol": s, "reason": "down"} for s in
                  ("000001.SZ", "000002.SZ", "000003.SZ", "510300.SH")]
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill(
        [], failed=all_failed, early=False))
    t = _mk_task()
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "failed"
    assert out["done_symbols"] == 0
    assert out["rows_written"] == 0


def test_execute_zero_rows_counts_done(seeded_db, monkeypatch):
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill([], rows=0))
    t = _mk_task()
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "ok"
    assert out["rows_written"] == 0


def test_retry_repulls_only_failed(seeded_db, monkeypatch):
    calls = []
    failed = [{"symbol": "000001.SZ", "reason": "boom"}]
    monkeypatch.setattr(
        tasks_mod, "backfill_pool",
        _mock_backfill(calls, failed=failed, rows=5),
    )
    t = _mk_task()
    tasks_mod.execute_task(t["task_id"])
    assert len(calls) == 2
    calls.clear()
    # 重试：mock 换成全成功，只补漏 000001.SZ（ETF 已 done，不重拉）
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill(calls, rows=2))
    out = tasks_mod.retry_task(t["task_id"])
    assert out["status"] == "ok"
    assert calls == [{"pool": [("000001.SZ", date(2024, 12, 31))],
                      "start": date(2016, 1, 1), "end": date(2024, 12, 31)}]
    assert out["done_symbols"] == 4


def test_retry_rejects_ok_task(seeded_db, monkeypatch):
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill([], rows=1))
    t = _mk_task()
    tasks_mod.execute_task(t["task_id"])
    with pytest.raises(ValueError, match="不可 retry"):
        tasks_mod.retry_task(t["task_id"])
    # 专类型断言：TaskConflictError（ValueError 子类，旧捕获方不受影响）
    with pytest.raises(tasks_mod.TaskConflictError):
        tasks_mod.retry_task(t["task_id"])


def test_retry_conflict_when_already_claimed(seeded_db, monkeypatch):
    """原子认领：已被抢先（状态不再 retriable）→ TaskConflictError，不重复执行。"""
    monkeypatch.setattr(tasks_mod, "backfill_pool", _mock_backfill([], rows=1))
    t = _mk_task()
    tasks_mod.execute_task(t["task_id"])          # → ok
    from lquant.core.db import writer

    with writer() as con:
        con.execute("UPDATE data_task SET status='running' WHERE task_id=?",
                    [t["task_id"]])
    with pytest.raises(tasks_mod.TaskConflictError):
        tasks_mod.retry_task(t["task_id"])
    # claim_retry 直接认领 partial 任务 → running，二次认领失败
    with writer() as con:
        con.execute("UPDATE data_task SET status='partial' WHERE task_id=?",
                    [t["task_id"]])
    tasks_mod.claim_retry(t["task_id"])
    with pytest.raises(tasks_mod.TaskConflictError):
        tasks_mod.claim_retry(t["task_id"])
    assert tasks_mod.get_task(t["task_id"])["status"] == "running"


def test_mark_interrupted(seeded_db):
    tasks_mod.create_task("daily_update", {"days": 10})
    _mk_running_row()
    n = tasks_mod.mark_interrupted_on_startup()
    assert n == 2
    t = tasks_mod.get_task("zzrunning01")
    assert t["status"] == "interrupted"


def test_get_and_list_tasks(seeded_db):
    t1 = _mk_task()
    # pending 也纳入互斥：活动任务存在时不能再创建
    with pytest.raises(tasks_mod.TaskConflictError):
        tasks_mod.create_task("daily_update", {"days": 10})
    from lquant.core.db import writer

    with writer() as con:
        con.execute("UPDATE data_task SET status='ok' WHERE task_id=?",
                    [t1["task_id"]])
    tasks_mod.create_task("daily_update", {"days": 10})
    assert tasks_mod.get_task(t1["task_id"])["task_id"] == t1["task_id"]
    assert tasks_mod.get_task("nope") is None
    lst = tasks_mod.list_tasks()
    assert len(lst) == 2
    assert all(set(t) >= _TASK_KEYS for t in lst)


_TASK_KEYS = {
    "task_id", "kind", "params", "status", "phase", "total_symbols",
    "done_symbols", "failed_symbols", "failed_detail", "rows_written",
    "started_at", "finished_at", "message",
}


def test_execute_crash_marks_failed(seeded_db, monkeypatch):
    def boom(pool, start, end=None, on_progress=None, **kw):
        raise RuntimeError("provider 起不来了")
    monkeypatch.setattr(tasks_mod, "backfill_pool", boom)
    t = _mk_task()
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "failed"
    assert "provider 起不来了" in out["message"]
    t2 = _mk_task()
    assert t2["status"] == "pending"


def test_progress_visible_midrun(seeded_db, monkeypatch):
    failed = [{"symbol": "000001.SZ", "reason": "boom"}]
    monkeypatch.setattr(
        tasks_mod, "backfill_pool",
        _mock_backfill([], failed=failed, rows=4),
    )
    t = _mk_task()
    mid = {}
    def spy(fn):
        def inner(pool, start, end=None, on_progress=None, **kw):
            def cb(frame):
                on_progress(frame)
                mid.update(tasks_mod.get_task(t["task_id"]))
            return fn(pool, start, end, on_progress=cb, **kw)
        return inner
    monkeypatch.setattr(tasks_mod, "backfill_pool", spy(tasks_mod.backfill_pool))
    out = tasks_mod.execute_task(t["task_id"])
    assert out["status"] == "partial"
    assert mid["done_symbols"] >= 3
    assert mid["phase"] in ("stocks", "etf")
    assert mid["failed_symbols"] == ["000001.SZ"]
    assert mid["failed_detail"] == [{"symbol": "000001.SZ", "reason": "boom"}]
