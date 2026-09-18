"""日线湖覆盖度对账（coverage scan）：缺口扫描 + issue 落库 + repair 任务。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立湖 + 独立目录库（LQ_ROOT + chdir + settings 缓存清理）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_calendar(start: date, end: date) -> list[date]:
    """工作日全开市，返回窗口内交易日列表。"""
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        d, rows = start, []
        while d <= end:
            if d.weekday() < 5:
                rows.append(d)
            d += timedelta(days=1)
        cal = pl.DataFrame(
            {"trade_date": rows, "is_open": [True] * len(rows)},
            schema_overrides={"trade_date": pl.Date},
        )
        con.register("_cal", cal)
        con.execute("INSERT INTO trade_calendar "
                    "SELECT trade_date, is_open, 'test' FROM _cal")
    return rows


def _seed_securities(symbols: list[str]) -> None:
    from lquant.data.store.catalog import SecurityRepo

    SecurityRepo().upsert(pl.DataFrame({
        "symbol": symbols,
        "sec_type": ["stock"] * len(symbols),
        "list_date": [date(2000, 1, 1)] * len(symbols),
    }))


def _daily_df(symbols: list[str], dates: list[date]) -> pl.DataFrame:
    n = len(symbols) * len(dates)
    return pl.DataFrame({
        "symbol": symbols * len(dates),
        "trade_date": dates * len(symbols),
        "close": [1.0] * n,
    })


def _gap_issues() -> list[dict]:
    from lquant.data.quality.issues import latest_issues

    return [i for i in latest_issues(200) if i["rule_code"] == "COVERAGE_GAP"]


def test_no_gap(env) -> None:
    from lquant.data.quality.coverage import scan_coverage
    from lquant.data.store.parquet import write_daily, write_daily_basic

    start, end = date(2026, 9, 7), date(2026, 9, 11)
    days = _seed_calendar(start, end)
    syms = ["600000.SH", "000001.SZ", "510300.SH"]
    _seed_securities(syms)
    write_daily(_daily_df(syms, days))
    write_daily_basic(_daily_df(syms, days))

    from lquant.core.types import today_cn

    rep = scan_coverage(30, repair=True)
    # 窗口右端＝业务日（CN 墙钟），不是进程本地 date.today()
    assert rep["window"]["end"] == today_cn()
    assert (rep["window"]["end"] - rep["window"]["start"]).days == 30
    assert rep["tables"]["daily"]["missing_dates"] == []
    assert rep["tables"]["daily"]["sparse_symbols"] == {}
    assert rep["tables"]["daily_basic"]["missing_dates"] == []
    assert rep["issues_recorded"] == 0
    assert rep["repair"] == {"created": False, "task_id": None, "reason": "no_gap"}


def test_daily_date_gap_creates_repair(env) -> None:
    from lquant.data.ingest.tasks import list_tasks
    from lquant.data.quality.coverage import scan_coverage
    from lquant.data.store.parquet import write_daily

    start, end = date(2026, 9, 7), date(2026, 9, 11)
    days = _seed_calendar(start, end)
    syms = ["600000.SH", "000001.SZ", "510300.SH"]
    _seed_securities(syms)
    write_daily(_daily_df(syms, days[1:]))  # 第一天整缺

    rep = scan_coverage(30, repair=True)
    assert rep["tables"]["daily"]["missing_dates"] == [days[0]]
    issues = _gap_issues()
    assert any(i["symbol"] is None and i["trade_date"] == days[0].isoformat()
               for i in issues)
    assert rep["issues_recorded"] > 0
    assert rep["repair"]["created"] is True
    task = next(t for t in list_tasks(20) if t["kind"] == "daily_update")
    assert task["params"]["start"] == days[0].isoformat()


def test_symbol_sparse_gap(env) -> None:
    from lquant.data.quality.coverage import scan_coverage
    from lquant.data.store.parquet import write_daily

    start, end = date(2026, 9, 7), date(2026, 9, 11)
    days = _seed_calendar(start, end)
    _seed_securities(["600000.SH", "000001.SZ"])
    write_daily(_daily_df(["600000.SH"], days))
    write_daily(_daily_df(["000001.SZ"], days[:2]))  # 后 3 天缺

    rep = scan_coverage(30)
    assert rep["tables"]["daily"]["missing_dates"] == []
    assert rep["tables"]["daily"]["sparse_symbols"] == {
        "000001.SZ": days[2:],
    }
    issue = next(i for i in _gap_issues() if i["symbol"] == "000001.SZ")
    assert issue["count"] == 3
    detail = issue["detail"]  # latest_issues 已把 detail JSON 解析成 dict
    assert issue["severity"] == "error"
    assert detail["dates"] == [d.isoformat() for d in days[2:]]


def test_basic_empty_lake_info_no_repair(env) -> None:
    """daily_basic 整湖为空是常态：info 级、不触发 repair。"""
    from lquant.data.quality.coverage import scan_coverage
    from lquant.data.store.parquet import write_daily

    start, end = date(2026, 9, 7), date(2026, 9, 11)
    days = _seed_calendar(start, end)
    syms = ["600000.SH", "000001.SZ"]
    _seed_securities(syms)
    write_daily(_daily_df(syms, days))

    rep = scan_coverage(30, repair=True)
    assert rep["tables"]["daily"]["missing_dates"] == []
    basic = rep["tables"]["daily_basic"]
    assert basic["missing_dates"] == days
    issues = _gap_issues()
    assert all(i["severity"] == "info"
               for i in issues if i["dataset"] == "daily_basic")
    assert rep["repair"]["reason"] == "no_gap"
    assert rep["repair"]["created"] is False
