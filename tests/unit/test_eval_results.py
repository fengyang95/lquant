"""server/eval_results 单元测试（fake_settings 隔离 tmp duckdb）。"""

from __future__ import annotations

import pytest

from lquant.server.eval_results import get_result, list_results, save_result


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_result_missing_table_returns_none(fake_settings):
    assert get_result("nope") is None


def test_list_results_missing_table_returns_empty(fake_settings):
    assert list_results("eval") == []


def test_save_and_get_roundtrip(fake_settings):
    save_result("j1", "eval", {"p": "参数"}, {"score": 0.9, "note": "中文"})
    got = get_result("j1")
    assert got["job_id"] == "j1" and got["kind"] == "eval"
    assert got["params"] == {"p": "参数"}
    assert got["result"] == {"score": 0.9, "note": "中文"}
    assert got["ts"] is not None


def test_save_result_idempotent_replace(fake_settings):
    save_result("j1", "eval", {"a": 1}, {"v": 1})
    save_result("j1", "eval", {"a": 2}, {"v": 2})
    got = get_result("j1")
    assert got["params"] == {"a": 2} and got["result"] == {"v": 2}


def test_list_results_order_and_kind_filter(fake_settings):
    save_result("a", "eval", {}, 1)
    save_result("b", "scan", {}, 2)
    save_result("c", "eval", {}, 3)
    rows = list_results("eval")
    assert [r["job_id"] for r in rows] == ["c", "a"] or \
        [r["job_id"] for r in rows] == ["a", "c"]
    assert all(r["kind"] == "eval" for r in rows)
    # limit 生效
    save_result("d", "eval", {}, 4)
    assert len(list_results("eval", limit=2)) == 2


def test_get_result_nonexistent_returns_none(fake_settings):
    save_result("x", "eval", {}, 1)
    assert get_result("missing") is None
