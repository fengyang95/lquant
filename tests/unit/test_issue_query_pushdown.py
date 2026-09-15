"""query_issues 检索：过滤下推 SQL + LIMIT 语义（不再全量拉内存过滤）。"""
from __future__ import annotations

import pytest


@pytest.fixture
def issue_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed(n_sev: dict) -> None:
    """按 {severity: n} 各落 n 条 issue。"""
    from lquant.data.quality.issues import Issue, save_issues

    issues = [Issue(rule=f"{sev}-R{i}", severity=sev, dataset="daily_bar",
                    detail="x") for sev, n in n_sev.items() for i in range(n)]
    save_issues(issues)


def test_query_issues_pushdown_and_limit(issue_env):
    _seed({"fatal": 1, "error": 3, "warn": 2})
    from lquant.data.quality.issues import query_issues

    all_err = query_issues(severity="error", limit=100)
    assert len(all_err) == 3
    assert all(r["severity"] == "error" for r in all_err)
    assert query_issues(severity="error", limit=2) == \
        all_err[:2]  # LIMIT 在 SQL 层截断，顺序稳定
    assert len(query_issues(dataset="daily_bar")) == 6
    assert query_issues(dataset="no-such") == []
    # resolved 过滤仍然生效
    first = all_err[0]["issue_id"]
    from lquant.data.quality.issues import resolve_issue

    resolve_issue(first)
    assert len(query_issues(severity="error")) == 2
    assert query_issues(severity="error", resolved=True)[0]["issue_id"] == first
