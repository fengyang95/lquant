"""market/scheduler 覆盖补齐：打桩注册表方法与 db 连接，不触网、不开真库。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

import lquant.market.scheduler as sched


class _Ctx:
    """可 with 的假连接（SimpleNamespace 不支持 with）。"""

    def __init__(self, con):
        self._con = con

    def __enter__(self):
        return self._con

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fake_collectors(monkeypatch):
    """打桩 COLLECTORS 的 meta/get：a/b 属 close（b 关键），c 属 evening。"""
    ok_a = lambda trade_date=None, demo=False: pl.DataFrame({"x": [1]})  # noqa: E731
    ok_b = lambda trade_date=None, demo=False: pl.DataFrame({"x": [1, 2]})  # noqa: E731
    empty_c = lambda trade_date=None, demo=False: pl.DataFrame()  # noqa: E731

    def meta(k):
        if k == "limit_up_pool":
            return {"name": "a", "schedule": "close", "table": "t_a"}
        if k == "limit_down_pool":
            return {"name": "b", "schedule": "close", "table": "t_b", "critical": True}
        if k == "northbound":
            return {"name": "c", "schedule": "evening", "table": "t_c"}
        return {"name": k, "schedule": "other", "table": None}

    def get(k):
        return {"limit_up_pool": ok_a, "limit_down_pool": ok_b,
                "northbound": empty_c}.get(k, empty_c)

    monkeypatch.setattr(sched.COLLECTORS, "meta", meta)
    monkeypatch.setattr(sched.COLLECTORS, "get", get)
    monkeypatch.setattr(sched, "_log_one", lambda *a, **kw: None)
    return meta, get


@pytest.fixture()
def fake_db(monkeypatch):
    """打桩 lquant.core.db 的 writer/reader（scheduler 内部延迟导入）。"""
    import lquant.core.db as db_mod

    conn = SimpleNamespace()

    def writer():
        return _Ctx(conn)

    def reader():
        return _Ctx(conn)

    monkeypatch.setattr(db_mod, "writer", writer)
    monkeypatch.setattr(db_mod, "reader", reader)
    monkeypatch.setattr(sched, "ensure_market_tables", lambda con: None)
    return conn


def test_due_schedules():
    assert sched.due_schedules(datetime(2026, 1, 1, 8, 0)) == []
    out = sched.due_schedules(datetime(2026, 1, 1, 15, 30))
    assert "preopen" in out and "intraday" in out and "close" in out
    assert "evening" not in out
    assert sched.due_schedules(datetime(2026, 1, 1, 23, 59)) == list(sched.SCHEDULES)


def test_collect_filters_by_schedule(fake_collectors):
    out = sched.collect(schedule="close")
    assert set(out) == {"limit_up_pool", "limit_down_pool"}
    out2 = sched.collect()
    assert {"limit_up_pool", "limit_down_pool", "northbound"} <= set(out2)
    out3 = sched.collect(only=["limit_up_pool"])
    assert set(out3) == {"limit_up_pool"}


def test_collect_swallows_non_critical_error(monkeypatch, fake_collectors):
    meta, get = fake_collectors

    def boom(**kw):
        raise ValueError("net down")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: boom if k == "limit_up_pool" else get(k))
    out = sched.collect(schedule="close")
    assert "limit_up_pool" not in out and "limit_down_pool" in out


def test_collect_critical_raises(monkeypatch, fake_collectors):
    meta, get = fake_collectors

    def boom(**kw):
        raise ValueError("net down")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: boom if k == "limit_down_pool" else get(k))
    with pytest.raises(RuntimeError, match="关键采集器"):
        sched.collect(schedule="close")


def test_collect_type_error_fallback(monkeypatch, fake_collectors):
    """采集器不接受 trade_date 时退化为只传 demo。"""
    meta, get = fake_collectors

    def strict(demo=False):
        return pl.DataFrame({"x": [9]})

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: strict if k == "limit_up_pool" else get(k))
    out = sched.collect(schedule="close")
    assert out["limit_up_pool"]["x"].to_list() == [9]


def test_persist_empty_and_missing_table(monkeypatch, fake_db, fake_collectors):
    assert sched.persist({}) == {}
    monkeypatch.setattr(sched, "TABLE_COLUMNS", {}, raising=False)
    counts = sched.persist({"northbound": pl.DataFrame({"x": [1]})})
    assert counts == {"northbound": 0}  # 表不在 TABLE_COLUMNS → cols 为空
    # 空 DataFrame 分支
    assert sched.persist({"limit_up_pool": pl.DataFrame()}) == {"limit_up_pool": 0}


def test_status_table_error_branch(monkeypatch, fake_db):
    """第二张表查询抛错 → 该行降级为 0 行覆盖（status 不该被单表炸掉）。"""
    calls = {"n": 0}

    def execute(sql):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("no table")
        return SimpleNamespace(fetchone=lambda: (5, "2024-01-02", "2026-06-30"))

    fake_db.execute = execute
    monkeypatch.setattr(sched, "TABLE_COLUMNS",
                        {"t_a": [], "t_b": []}, raising=False)
    df = sched.status()
    assert df["rows"].to_list() == [5, 0]
    assert df["last_day"].to_list()[1] is None


def test_persist_success_and_upsert_fail(monkeypatch, fake_db, fake_collectors):
    calls = []

    def fake_upsert(table, df):
        calls.append(table)
        if table == "t_b":
            raise RuntimeError("write fail")
        return len(df)

    monkeypatch.setattr(sched, "upsert", fake_upsert)
    monkeypatch.setattr(sched, "TABLE_COLUMNS",
                        {"t_a": ["x"], "t_b": ["x"]}, raising=False)
    counts = sched.persist({"limit_up_pool": pl.DataFrame({"x": [1]}),
                            "limit_down_pool": pl.DataFrame({"x": [1, 2]})})
    assert counts == {"limit_up_pool": 1, "limit_down_pool": 0}
    assert calls == ["t_a", "t_b"]


def test_collect_and_save_success(monkeypatch, fake_db, fake_collectors):
    monkeypatch.setattr(sched, "upsert", lambda table, df: len(df))
    monkeypatch.setattr(sched, "TABLE_COLUMNS",
                        {"t_a": ["x"], "t_b": ["x"]}, raising=False)
    out = sched.collect_and_save(schedule="close", demo=True)
    assert out["collected"] == {"limit_up_pool": 1, "limit_down_pool": 2}
    assert out["persisted"] == {"limit_up_pool": 1, "limit_down_pool": 2}
    assert out["errors"] == {}


def test_collect_and_save_critical_failure(monkeypatch, fake_collectors):
    meta, get = fake_collectors

    def boom(**kw):
        raise ValueError("net down")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: boom if k == "limit_down_pool" else get(k))
    with pytest.raises(RuntimeError, match="关键采集器"):
        sched.collect_and_save(schedule="close")


def test_collect_and_save_non_critical_error(monkeypatch, fake_db, fake_collectors):
    meta, get = fake_collectors
    monkeypatch.setattr(sched, "upsert", lambda table, df: len(df))
    monkeypatch.setattr(sched, "TABLE_COLUMNS", {"t_c": ["x"]}, raising=False)

    def boom(**kw):
        raise ValueError("net down")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: boom if k == "northbound" else get(k))
    out = sched.collect_and_save(schedule="evening")
    assert out["errors"]["northbound"].startswith("ValueError")
    assert out["collected"]["northbound"] == 0


def test_collect_failure_also_lands_in_quality_issues(monkeypatch, fake_db, fake_collectors):
    """采集失败要同时进 data_quality_issue。

    只落在 collect_log（运行视角）里的失败，看数据的人看不到 —— 北向那次
    源站停发字段后采集器把 0 当值写库，collect_log 全程 "ok"。
    """
    from lquant.data.quality import issues as qissues

    meta, get = fake_collectors
    monkeypatch.setattr(sched, "upsert", lambda table, df: len(df))
    monkeypatch.setattr(sched, "TABLE_COLUMNS", {"t_c": ["x"]}, raising=False)

    def boom(**kw):
        raise ValueError("net down")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: boom if k == "northbound" else get(k))
    seen = []
    monkeypatch.setattr(qissues, "save_issues", lambda items, *a, **k: seen.extend(items))

    sched.collect_and_save(schedule="evening")
    rule = [i.rule for i in seen if i.dataset == "northbound"]
    assert rule == ["COLLECTOR_FAILED"]
    assert seen[0].severity == "error"

    # demo 跑出来的失败不入质量问题表
    seen.clear()
    sched.collect_and_save(schedule="evening", demo=True)
    assert seen == []


def test_status_rows(monkeypatch, fake_db):
    def execute(sql):
        return SimpleNamespace(fetchone=lambda: (5, "2024-01-02", "2026-06-30"))

    fake_db.execute = execute
    monkeypatch.setattr(sched, "TABLE_COLUMNS",
                        {"t_a": [], "t_b": []}, raising=False)
    df = sched.status()
    assert df["rows"].to_list() == [5, 5]
    assert df["first_day"].to_list()[0] == "2024-01-02"


def test_collect_type_error_then_fail(monkeypatch, fake_collectors):
    """采集器不收 trade_date 且连 demo-only 调用也失败 → 记入 errors。"""
    meta, get = fake_collectors

    def always_wrong(*a, **kw):
        raise TypeError("bad signature")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: always_wrong if k == "limit_up_pool" else get(k))
    out = sched.collect(schedule="close")
    assert "limit_up_pool" not in out and "limit_down_pool" in out


def test_log_one_records_and_swallows(monkeypatch):
    """_log_one 正常落 collect_log；record 抛错也不影响主流程。"""
    import lquant.market.collect_log as cl_mod

    calls = []
    monkeypatch.setattr(cl_mod, "record",
                        lambda *a, **kw: calls.append(a))
    sched._log_one("job", "2026-01-01", datetime(2026, 1, 1), datetime(2026, 1, 1), 3, "ok")
    assert len(calls) == 1

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(cl_mod, "record", boom)
    sched._log_one("job", "2026-01-01", datetime(2026, 1, 1), datetime(2026, 1, 1), 0, "failed")
    assert len(calls) == 1


def test_collect_failure_issue_write_failure_is_swallowed(monkeypatch, fake_db, fake_collectors):
    """留痕写库失败不能把采集主流程带崩。"""
    from lquant.data.quality import issues as qissues

    meta, get = fake_collectors
    monkeypatch.setattr(sched, "upsert", lambda table, df: len(df))
    monkeypatch.setattr(sched, "TABLE_COLUMNS", {"t_c": ["x"]}, raising=False)

    def boom(**kw):
        raise ValueError("net down")

    def save_boom(*_a, **_k):
        raise RuntimeError("issue 表坏了")

    monkeypatch.setattr(sched.COLLECTORS, "get",
                        lambda k: boom if k == "northbound" else get(k))
    monkeypatch.setattr(qissues, "save_issues", save_boom)
    out = sched.collect_and_save(schedule="evening")
    assert out["errors"]["northbound"].startswith("ValueError")   # 采集结果照常返回
