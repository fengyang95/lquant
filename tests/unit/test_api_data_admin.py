"""数据管理增强端点：issue 检索/批量 resolve、data_version、purge、export。

同 test_api_data_tasks.py 模式：tmp 目录 + generate_demo 自包含合成环境，
全链路离线。purge/export 直接操作 tmp 湖里的日线 parquet，验证物理效果。
"""
from __future__ import annotations

import datetime as dt
import io
import os

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")

# 控制湖内容的两个测试标的（write_daily 直写，绕开网络）
_SYMS = ["600100.SH", "000200.SZ"]


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_data_admin")
    prev_cwd = os.getcwd()  # 模块级 fixture 必须还原 CWD，否则污染后续测试文件
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    _seed_lake()
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


def _seed_lake() -> None:
    """往 tmp 湖写两年小日线：600100 两年都有，000200 只有 2024。"""
    from lquant.data.store.parquet import write_daily

    rows = []
    for sym in _SYMS:
        for year, days in ((2024, 5), (2025, 5)):
            if sym == "000200.SZ" and year == 2025:
                continue
            for d in range(1, days + 1):
                rows.append({
                    "symbol": sym,
                    "trade_date": dt.date(year, 1, d),
                    "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                    "volume": 1000.0, "amount": 10500.0,
                })
    df = pl.DataFrame(rows, schema_overrides={"trade_date": pl.Date})
    write_daily(df)


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------- 1. issue 检索 / 批量 resolve ----------

def _seed_issues() -> list[str]:
    from lquant.data.quality.issues import Issue, save_issues

    save_issues([
        Issue(rule="R1", severity="error", detail="d1", dataset="daily_bar",
              symbol="600100.SH", trade_date=dt.date(2024, 1, 2)),
        Issue(rule="R2", severity="warn", detail="d2", dataset="minute_bar",
              symbol="000200.SZ", trade_date=dt.date(2024, 1, 3)),
    ])
    from lquant.data.quality.issues import latest_issues

    return [r["issue_id"] for r in latest_issues(limit=10)]


def test_issues_filter_by_severity(client):
    ids = _seed_issues()
    assert len(ids) >= 2
    rows = client.get("/api/data/issues", params={"severity": "error"}).json()
    assert rows and all(r["severity"] == "error" for r in rows)
    rows = client.get("/api/data/issues", params={"dataset": "minute_bar"}).json()
    assert rows and all(r["dataset"] == "minute_bar" for r in rows)
    rows = client.get("/api/data/issues", params={"resolved": True}).json()
    assert rows == []


def test_issues_resolve_batch(client):
    ids = _seed_issues()
    r = client.post("/api/data/issues/resolve", json={"ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] == len(ids)
    after = client.get("/api/data/issues", params={"resolved": True}).json()
    got = {x["issue_id"] for x in after}
    assert set(ids) <= got


def test_issues_resolve_missing_id_404(client):
    r = client.post("/api/data/issues/resolve", json={"ids": ["deadbeef" * 3]})
    assert r.status_code == 404


def test_issues_resolve_empty_ids_422(client):
    r = client.post("/api/data/issues/resolve", json={"ids": []})
    assert r.status_code == 422


# ---------- 2. data_version 端点 ----------

def test_version_latest_empty(client):
    r = client.get("/api/data/version/latest")
    assert r.status_code == 200
    assert r.json() is None  # 从未登记版本不是错误


def test_version_latest_and_list(client):
    from lquant.data.lineage import register

    register("20990101.1", "daily_bar", 100)
    register("20990101.2", "daily_bar", 200)
    register("20990101.3", "daily_basic", 50)

    r = client.get("/api/data/version/latest", params={"dataset": "daily_bar"})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "20990101.2"
    assert body["dataset"] == "daily_bar"
    assert "created_at" in body and "row_count" in body

    rows = client.get("/api/data/versions", params={"limit": 2}).json()
    assert len(rows) == 2
    assert rows[0]["version"] == "20990101.3"  # 最新在前


# ---------- 3. purge ----------

def _lake_rows() -> int:
    from lquant.data.store.parquet import read_daily

    return read_daily().select("symbol").collect().height


def test_purge_no_filter_422(client):
    r = client.post("/api/data/purge", json={"dataset": "daily", "dry_run": True})
    assert r.status_code == 422


def test_purge_dry_run_keeps_lake(client):
    before = _lake_rows()
    r = client.post("/api/data/purge", json={
        "dataset": "daily", "symbols": ["600100.SH"], "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["rows_matched"] == 10  # 600100 两年各 5 天
    assert _lake_rows() == before  # dry_run 不落盘


def test_purge_by_symbol(client):
    before = _lake_rows()
    r = client.post("/api/data/purge", json={
        "dataset": "daily", "symbols": ["000200.SZ"], "dry_run": False})
    assert r.status_code == 200
    assert r.json()["rows_matched"] == 5
    assert _lake_rows() == before - 5


def test_purge_by_date_range(client):
    before = _lake_rows()
    r = client.post("/api/data/purge", json={
        "dataset": "daily", "start": "2025-01-01", "end": "2025-12-31",
        "dry_run": False})
    assert r.status_code == 200
    assert r.json()["rows_matched"] == 5  # 只剩 600100 的 2025
    assert _lake_rows() == before - 5


def test_purge_bad_date_422(client):
    r = client.post("/api/data/purge", json={
        "dataset": "daily", "start": "not-a-date", "dry_run": True})
    assert r.status_code == 422


def test_purge_unknown_dataset_422(client):
    r = client.post("/api/data/purge", json={
        "dataset": "nope", "symbols": ["600100.SH"], "dry_run": True})
    assert r.status_code == 422


# ---------- 4. export ----------

def test_export_csv(client):
    r = client.get("/api/data/export", params={
        "dataset": "daily", "symbols": "600100.SH",
        "start": "2024-01-01", "end": "2024-12-31", "format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.csv"')
    text = r.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert "symbol" in header and "trade_date" in header
    assert len(text.splitlines()) == 6  # 表头 + 5 行


def test_export_parquet(client):
    # 注意：purge 用例会真实删湖（2025 段被清），导出用例都显式限定 2024
    r = client.get("/api/data/export", params={
        "dataset": "daily", "symbols": "600100.SH",
        "start": "2024-01-01", "end": "2024-12-31", "format": "parquet"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    df = pl.read_parquet(io.BytesIO(r.content))
    assert df.height == 5
    assert set(df["symbol"].unique()) == {"600100.SH"}


def test_export_bad_format_422(client):
    r = client.get("/api/data/export", params={"dataset": "daily", "format": "xlsx"})
    assert r.status_code == 422


def test_export_bad_dataset_422(client):
    r = client.get("/api/data/export", params={"dataset": "minute", "format": "csv"})
    assert r.status_code == 422


def test_export_bad_date_422(client):
    r = client.get("/api/data/export", params={
        "dataset": "daily", "start": "2024/01/01", "format": "csv"})
    assert r.status_code == 422


# ---------- 5. 告警阈值可配置 ----------

def test_coverage_monthly_has_threshold(client):
    body = client.get("/api/data/coverage/monthly").json()
    assert body["threshold"] == 30  # 默认值
    # PUT 修改后立即生效
    from lquant.core.settings_store import SettingsStore

    SettingsStore().put("coverage_drop_warn_pct", "45")
    try:
        body = client.get("/api/data/coverage/monthly").json()
        assert body["threshold"] == 45
    finally:
        SettingsStore().reset("coverage_drop_warn_pct")


def test_coverage_threshold_setting_registered():
    from lquant.core.settings_store import SETTING_DEFS, coerce_setting

    assert "coverage_drop_warn_pct" in SETTING_DEFS
    v, err = coerce_setting("coverage_drop_warn_pct", "40")
    assert (v, err) == ("40", None)
    v, err = coerce_setting("coverage_drop_warn_pct", "abc")
    assert err


# ---------- 6. 数据字典 ----------

def test_dictionary(client):
    body = client.get("/api/data/dictionary").json()
    assert "daily_bar" in body
    entry = body["daily_bar"]
    assert entry["label"]
    assert entry["fields"]["symbol"]
    assert entry["fields"]["trade_date"]
    # SCHEMAS 里的每张表都要覆盖
    from lquant.data.schema import SCHEMAS

    for name in SCHEMAS:
        assert name in body, f"字典缺表 {name}"
        missing = set(SCHEMAS[name]) - set(body[name]["fields"])
        assert not missing, f"字典 {name} 缺字段 {missing}"
