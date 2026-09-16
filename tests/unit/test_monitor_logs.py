"""监控运行日志 tail：格式解析 / 级别过滤 / 关键字搜索 / 多行合并。"""
from __future__ import annotations

import pytest

from lquant.monitor.logs import tail_app_logs


@pytest.fixture
def log_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_LOG_DIR", str(tmp_path))
    yield tmp_path


def _write(env, text: str) -> None:
    (env / "lquant.log").write_text(text, encoding="utf-8")


SAMPLE = (
    "2026-09-16 09:00:00.000 | INFO    | r1 | mod.f:1 - started\n"
    "2026-09-16 09:00:01.000 | WARNING | r1 | mod.f:2 - something warn\n"
    "2026-09-16 09:00:02.000 | ERROR   | r2 | mod.f:3 - boom\n"
    "  File \"x.py\", line 1\n"
    "ValueError: bad\n"
    "2026-10-01 10:00:00.000 | DEBUG   |     | mod.g:1 - debug line\n"
)


def test_parse_filter_level(log_env) -> None:
    _write(log_env, SAMPLE)
    # WARNING 及以上 → warn + error 两条，traceback 合并进 error 记录
    r = tail_app_logs(level="WARNING")
    assert r["total"] == 2
    assert r["items"][0]["message"] == "boom\n  File \"x.py\", line 1\nValueError: bad"
    assert r["items"][0]["level"] == "ERROR"
    assert "WARNING" in [r["level"] for r in r["items"]]


def test_search_keyword(log_env) -> None:
    _write(log_env, SAMPLE)
    r = tail_app_logs(q="boom")
    assert r["total"] == 1
    assert r["items"][0]["level"] == "ERROR"


def test_search_matches_continuation_lines(log_env) -> None:
    _write(log_env, SAMPLE)
    r = tail_app_logs(q="ValueError")
    assert r["total"] == 1
    assert "ValueError" in r["items"][0]["message"]


def test_limit_tail(log_env) -> None:
    lines = [
        f"2026-09-16 09:{m:02d}:{s:02d}.000 | INFO    |  | m:1 - msg{m}{s}\n"
        for m in range(2) for s in range(30)
    ]
    _write(log_env, "".join(lines))
    r = tail_app_logs(limit=10)
    assert r["total"] == 60
    assert len(r["items"]) == 10
    assert r["items"][0]["message"] == "msg129"   # 倒序：最新在前


def test_missing_file_returns_empty(log_env) -> None:
    r = tail_app_logs()
    assert r == {"items": [], "total": 0}


def test_api_endpoint_wiring(log_env) -> None:
    """API 端点直调：参数透传 + 响应契约（items/total）。"""
    _write(log_env, SAMPLE)
    from lquant.server.api.monitor import app_logs

    r = app_logs(level="WARNING", q="boom", limit=200)
    assert r["total"] == 1
    assert r["items"][0]["level"] == "ERROR"
    assert all("boom" in it["message"] for it in r["items"])
