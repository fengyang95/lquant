"""任务中心 qlib 归一测试。"""
import pytest
from fastapi import HTTPException

from lquant.server.api import task_center as tc


def test_kinds_contains_qlib():
    assert "qlib" in tc.KINDS


def test_job_items_qlib(monkeypatch):
    monkeypatch.setattr(tc, "list_recent_jobs",
                        lambda limit: [{"id": "j1", "queue": "lquant-qlib",
                                        "status": "finished",
                                        "created_at": 1.0}])
    items = tc._items("qlib", 10)
    assert items[0]["kind"] == "qlib"
    name = items[0]["name"]
    assert name in ("Qlib 任务", "Qlib 导出", "Qlib 工作流")


def test_items_unknown_raises():
    with pytest.raises(HTTPException):
        tc._items("bogus", 10)
