"""news_task 状态机单元测试(不联网,runner 注入)。

link 管线的关联输入(securities()/industry_classify)在无网环境下
按实现降级为空映射,不影响本测试断言。
"""
from __future__ import annotations

import duckdb
import pytest

from lquant.news.model import NewsItem
from lquant.news.store import init_news_ddl
from lquant.news.tasks import (
    TaskConflictError,
    create_task,
    execute_task,
    init_news_task_ddl,
    mark_interrupted_on_startup,
    retry_task,
)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_news_ddl(c)
    init_news_task_ddl(c)
    return c


def _ni(ext: str, name: str = "s1") -> NewsItem:
    return NewsItem(source="telegraph", source_name=name, external_id=ext,
                    title=ext, content="", url="u")


def test_partial_then_retry_only_failed(con):
    calls: list[str] = []
    s2_calls = 0

    def runner(day, src):
        nonlocal s2_calls
        calls.append(src)
        if src == "s1":
            return [_ni("a"), _ni("b")]
        s2_calls += 1
        if s2_calls > 1:                    # 重试时恢复成功
            return [_ni("c"), _ni("d")]
        raise RuntimeError("boom")

    t = create_task(con, "manual", {"sources": ["s1", "s2"]})
    out = execute_task(con, t["task_id"], runner=runner)
    assert out["status"] == "partial"
    assert out["rows_written"] == 2
    assert out["sources_status"]["s1"]["status"] == "ok"
    # 失败 source 有 error 明细
    assert "boom" in out["sources_status"]["s2"]["error"]

    out2 = retry_task(con, t["task_id"], runner=runner)
    assert calls.count("s1") == 1          # s1 未被重跑
    assert calls.count("s2") == 2          # s2 被重试一次
    assert out2["status"] == "ok"
    assert out2["rows_written"] == 2


def test_dedupe_rerun_zero_rows(con):
    def runner(day, src):
        return [_ni("a"), _ni("b")] if src == "s1" else []

    t = create_task(con, "manual", {"sources": ["s1"]})
    execute_task(con, t["task_id"], runner=runner)
    t2 = create_task(con, "manual", {"sources": ["s1"]})
    out = execute_task(con, t2["task_id"], runner=runner)
    assert out["rows_written"] == 0        # news_id 去重命中


def test_all_failed_and_conflict(con):
    def runner(day, src):
        raise RuntimeError("down")

    t = create_task(con, "manual", {"sources": ["s1"]})
    out = execute_task(con, t["task_id"], runner=runner)
    assert out["status"] == "failed"

    # 存在终态任务不互斥,但 pending/running 互斥
    t_pending = create_task(con, "manual", {"sources": ["s1"]})
    with pytest.raises(TaskConflictError):
        create_task(con, "manual", {"sources": ["s1"]})

    # failed 允许 retry
    out2 = retry_task(con, t["task_id"], runner=runner)
    assert out2["status"] == "failed"

    # 非 failed/partial/interrupted 状态不允许 retry
    con.execute("UPDATE news_task SET status='ok' WHERE task_id=?", [t["task_id"]])
    with pytest.raises(TaskConflictError):
        retry_task(con, t["task_id"], runner=runner)

    # pending 状态也不允许 retry
    with pytest.raises(TaskConflictError):
        retry_task(con, t_pending["task_id"], runner=runner)


def test_interrupted_on_startup(con):
    t = create_task(con, "manual", {"sources": ["s1"]})
    con.execute("UPDATE news_task SET status='running' WHERE task_id=?", [t["task_id"]])
    assert mark_interrupted_on_startup(con) == 1
    row = con.execute("SELECT status FROM news_task WHERE task_id=?",
                      [t["task_id"]]).fetchone()
    assert row[0] == "interrupted"

    # 幂等:再次执行返回 0
    assert mark_interrupted_on_startup(con) == 0


def test_invalid_kind_rejected(con):
    with pytest.raises(ValueError):
        create_task(con, "weekly", {"sources": ["s1"]})


def test_default_runner_from_registry(con, monkeypatch):
    """runner=None 走模块级 _default_runner(monkeypatch 锚点)。"""
    calls: list[tuple[str, str]] = []

    def fake_default(day, src):
        calls.append((str(day), src))
        return [_ni("x", name=src)]

    monkeypatch.setattr("lquant.news.tasks._default_runner", fake_default)
    t = create_task(con, "manual", {"sources": ["s1"]})
    out = execute_task(con, t["task_id"])  # 不传 runner
    assert out["status"] == "ok"
    assert calls and calls[0][1] == "s1"
