"""扩展数据 API 的契约测试（离线，tmp 数据根）。

只挂载 ``ext_data`` 路由，不起完整应用 —— 用例要验的是端点行为与错误码，
不是 lifespan/监控中间件。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lquant.data.ext import ExtConfigStore


@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv("LQ_DATA_DIR", str(root))
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "lq.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def client(api_env: Path) -> TestClient:
    from lquant.server.api import ext_data

    app = FastAPI()
    app.include_router(ext_data.router, prefix="/api")
    return TestClient(app)


def _create(client: TestClient, **overrides) -> dict:
    body = {
        "id": "heat", "label": "热度", "mode": "timeseries",
        "fields": [{"name": "heat", "dtype": "float"},
                   {"name": "concepts", "dtype": "string"}],
    }
    body.update(overrides)
    resp = client.post("/api/ext-data", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_list_write_rows_values(client: TestClient) -> None:
    _create(client)
    listing = client.get("/api/ext-data").json()
    assert [t["id"] for t in listing] == ["heat"]
    assert listing[0]["coverage"]["partitions"] == 0

    wrote = client.post("/api/ext-data/heat/write", json={
        "date": "2024-03-01",
        "rows": [
            {"symbol": "600000", "heat": 1.5, "concepts": "AI"},
            {"symbol": "000001", "heat": 2.5, "concepts": "银行"},
        ],
    })
    assert wrote.status_code == 200, wrote.text
    assert wrote.json()["rows"] == 2

    rows = client.get("/api/ext-data/heat/rows", params={"date": "2024-03-01"}).json()
    assert rows["total"] == 2 and rows["date"] == "2024-03-01"
    assert {r["symbol"] for r in rows["rows"]} == {"600000.SH", "000001.SZ"}

    paged = client.get("/api/ext-data/heat/rows",
                       params={"date": "2024-03-01", "offset": 1, "limit": 1}).json()
    assert paged["total"] == 2 and len(paged["rows"]) == 1

    filtered = client.get("/api/ext-data/heat/rows",
                          params={"date": "2024-03-01", "filter": "concepts~AI"}).json()
    assert filtered["total"] == 1
    assert filtered["rows"][0]["heat"] == 1.5

    values = client.get("/api/ext-data/heat/values",
                        params={"date": "2024-03-01", "field": "concepts"}).json()
    assert values["distinct"] == 2

    coverage = client.get("/api/ext-data").json()[0]["coverage"]
    assert coverage["partitions"] == 1
    assert coverage["date_range"] == ["2024-03-01", "2024-03-01"]


def test_unknown_table_is_404(client: TestClient) -> None:
    assert client.get("/api/ext-data/nope/rows").status_code == 404


def test_create_rejects_illegal_id(client: TestClient) -> None:
    resp = client.post("/api/ext-data", json={
        "id": "../daily", "label": "x", "mode": "snapshot",
        "fields": [{"name": "a", "dtype": "float"}],
    })
    assert resp.status_code == 422


def test_create_rejects_bad_dtype(client: TestClient) -> None:
    resp = client.post("/api/ext-data", json={
        "id": "x", "label": "x", "mode": "snapshot",
        "fields": [{"name": "a", "dtype": "decimal"}],
    })
    assert resp.status_code == 422


def test_write_missing_field_is_400(client: TestClient) -> None:
    _create(client)
    resp = client.post("/api/ext-data/heat/write", json={
        "date": "2024-03-01",
        "rows": [{"symbol": "600000", "heat": 1.0}],
    })
    assert resp.status_code == 400
    assert "缺少字段" in resp.json()["detail"]


def test_write_timeseries_without_date_is_400(client: TestClient) -> None:
    _create(client)
    resp = client.post("/api/ext-data/heat/write", json={
        "rows": [{"symbol": "600000", "heat": 1.0, "concepts": "AI"}],
    })
    assert resp.status_code == 400


def test_bad_date_is_400(client: TestClient) -> None:
    _create(client)
    resp = client.get("/api/ext-data/heat/rows", params={"date": "2024-13-99"})
    assert resp.status_code == 400


def test_upload_raw_body_csv(client: TestClient) -> None:
    _create(client)
    csv = b"symbol,heat,concepts\n600000,3.5,AI\n"
    resp = client.post(
        "/api/ext-data/heat/upload",
        params={"filename": "u.csv", "date": "2024-03-04"},
        content=csv,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == 1
    rows = client.get("/api/ext-data/heat/rows", params={"date": "2024-03-04"}).json()
    assert rows["rows"][0]["heat"] == 3.5


def test_upload_unsupported_suffix_is_400(client: TestClient) -> None:
    _create(client)
    resp = client.post(
        "/api/ext-data/heat/upload", params={"filename": "u.parquet"},
        content=b"x", headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 400


def test_market_level_table_rows_without_symbol(client: TestClient) -> None:
    _create(client, id="mkt", label="市场情绪", market_level=True,
            symbol_field=None, fields=[{"name": "score", "dtype": "float"}])
    resp = client.post("/api/ext-data/mkt/write", json={
        "date": "2024-03-01", "rows": [{"score": 0.5}],
    })
    assert resp.status_code == 200, resp.text
    rows = client.get("/api/ext-data/mkt/rows", params={"date": "2024-03-01"}).json()
    assert rows["total"] == 1 and "symbol" not in rows["rows"][0]


def test_backfill_rejects_mismatched_dates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通过 API 复现「接口忽略 date 参数 → 历史被拒绝写入」。"""
    _create(client, date_param="date", pull={
        "url": "https://example.test/heat", "response_path": "data.list",
    })

    def ignores_date(url: str, **_kw):
        return {"data": {"list": [
            {"date": "2024-06-01", "symbol": "600000", "heat": 9.9, "concepts": "AI"}
        ]}}

    # fetch_rows 在调用时解析 `fetcher or _default_fetcher`，替换模块属性即可
    # 让 API 走注入实现，全程不联网。
    import lquant.data.ext.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "_default_fetcher", ignores_date)

    resp = client.post("/api/ext-data/heat/backfill", json={
        "start": "2024-01-02", "end": "2024-01-04",
    })
    # trading_days 来自 trade_calendar；空库取不到交易日 → 400（显式报错）
    assert resp.status_code == 400
    assert "交易日" in resp.json()["detail"]

    # 直接驱动 backfill（显式给交易日）验证核心契约
    from datetime import date

    from lquant.data.ext import backfill

    cfg = ExtConfigStore().get("heat")
    report = backfill(cfg, date(2024, 1, 2), date(2024, 1, 4),
                      trading_days=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
                      fetcher=ignores_date)
    assert report["fetched"] == 0 and len(report["failed"]) == 3


def test_router_is_mounted_in_create_app(api_env: Path) -> None:
    """只验挂载：``/api/ext-data`` 必须出现在应用路由里。"""
    from lquant.server.main import create_app

    app = create_app()
    inner = getattr(app, "app", app)  # create_app 返回 MonitorMiddleware 包裹
    # 该版本 FastAPI 把 include_router 记成 _IncludedRouter 节点而不是拍平路径，
    # 用 OpenAPI 的 paths 判断挂载最稳。
    paths = set(inner.openapi()["paths"])
    assert "/api/ext-data" in paths
    assert "/api/ext-data/{table_id}/rows" in paths
