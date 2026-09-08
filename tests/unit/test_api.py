"""API 层集成测试 —— 拷贝真实 duckdb + parquet 湖到临时目录，全链路离线跑。

覆盖：health / data(coverage/securities/daily/indicators/quote) / watchlist CRUD /
factors 注册-列表-详情-校验 / backtests run-list-detail-compare / market collect 健康度 /
strategies 枚举 / ws 任务推送。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

LQ_ROOT = Path(__file__).resolve().parents[2]
# 测试环境不启动同步后台线程（避免测试期间触发真实采集）
os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    """每个测试模块拷一份隔离数据环境（duckdb 9.8M + parquet 0.5M）。"""
    base = tmp_path_factory.mktemp("api")
    os.chdir(base)
    (base / "data" / "duckdb").mkdir(parents=True, exist_ok=True)
    shutil.copy2(LQ_ROOT / "data" / "duckdb" / "lquant.duckdb",
                 base / "data" / "duckdb" / "lquant.duckdb")
    shutil.copytree(LQ_ROOT / "data" / "parquet", base / "data" / "parquet",
                    dirs_exist_ok=True)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield base
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:     # with 触发 startup DDL/迁移
        yield c


# ---------- 基础 ----------

def test_health(client):
    r = client.get("/api/health/ping")
    assert r.status_code == 200


def test_strategies_endpoint(client):
    """策略注册表枚举（扩展点：自定义策略注册后自动出现在这里）。"""
    r = client.get("/api/strategies")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "factor_topn" in names
    assert any(s.get("label") for s in r.json())


# ---------- data ----------

def test_data_coverage(client):
    r = client.get("/api/data/coverage")
    assert r.status_code == 200
    body = r.json()
    assert "daily_lake" in body and "tables" in body
    assert body["daily_lake"]["rows"] > 0            # 拷贝来的 demo 湖非空


def test_data_securities_and_daily(client):
    r = client.get("/api/data/securities", params={"q": "600519", "limit": 3})
    assert r.status_code == 200
    hits = r.json()
    assert hits and hits[0]["symbol"].startswith("600519")
    sym = hits[0]["symbol"]
    r2 = client.get("/api/data/daily", params={"symbol": sym, "limit": 5})
    assert r2.status_code == 200
    rows = r2.json()
    assert 0 < len(rows) <= 5
    assert {"trade_date", "open", "close"} <= set(rows[0])


def test_data_indicators(client):
    r = client.get("/api/data/indicators", params={"symbol": "600519.SH", "limit": 30})
    assert r.status_code == 200
    rows = r.json()
    assert rows and "rsi14" in rows[-1]


def test_data_quote_graceful(client):
    """实时行情：有网返回真数据，无网/沙箱必须优雅降级而不是 500。"""
    r = client.get("/api/data/quote", params={"symbol": "600519"})
    assert r.status_code == 200
    assert "available" in r.json()


# ---------- watchlist ----------

def test_watchlist_crud_roundtrip(client):
    client.delete("/api/watchlist/600519.SH")            # 拷贝库可能已含该票，先清
    r = client.post("/api/watchlist", json={"symbol": "600519", "note": "测试"})
    assert r.status_code == 200
    lst = client.get("/api/watchlist").json()
    assert any(w["symbol"] == "600519.SH" and w.get("name") for w in lst)
    assert client.delete("/api/watchlist/600519.SH").status_code == 200
    assert client.delete("/api/watchlist/600519.SH").status_code == 404


def test_watchlist_invalid_symbol_422(client):
    assert client.post("/api/watchlist", json={"symbol": "abc"}).status_code == 422


# ---------- factors ----------

def test_factor_register_list_detail_validation(client):
    assert client.post("/api/factors", json={
        "name": "tst_mom5", "expression": "Rank(Ts_Mean($close,5)/$close-1)",
    }).status_code == 200
    names = [f["name"] for f in client.get("/api/factors").json()]
    assert "tst_mom5" in names
    detail = client.get("/api/factors/tst_mom5")
    assert detail.status_code == 200
    assert detail.json()["expression"].startswith("Rank(")
    # 分页参数生效
    paged = client.get("/api/factors", params={"limit": 1, "offset": 0})
    assert len(paged.json()) == 1


def test_factor_bad_dsl_422(client):
    r = client.post("/api/factors", json={"name": "tst_bad", "expression": "Rank(Ts_Foo($close,5))"})
    assert r.status_code == 422


def test_factor_detail_404(client):
    assert client.get("/api/factors/no_such_factor").status_code == 404


def test_builtin_factors_list_and_filter(client):
    r = client.get("/api/factors/builtin")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 158                       # Qlib Alpha158 全量
    assert {"kbar", "price"} <= {x["family"] for x in items}
    r2 = client.get("/api/factors/builtin", params={"family": "ma"})
    assert {x["name"] for x in r2.json()} == {f"MA{d}" for d in (5, 10, 20, 30, 60)}
    r3 = client.get("/api/factors/builtin", params={"q": "RSV"})
    assert {x["name"] for x in r3.json()} == {f"RSV{d}" for d in (5, 10, 20, 30, 60)}


def test_seed_builtin_and_evaluate(client):
    r = client.post("/api/factors/seed-builtin",
                    json={"names": ["MA20", "RSV10"]})
    assert r.status_code == 200
    assert r.json()["seeded"] == 2
    names = [f["name"] for f in client.get("/api/factors").json()]
    assert {"MA20", "RSV10"} <= set(names)
    # 内置因子走完整评价管线
    ev = client.post("/api/factors/evaluate", json={
        "factor": "MA20", "formula": "MA20", "start": "2024-06-01"})
    assert ev.status_code == 200
    body = ev.json()
    assert "ic" in body and body["n_samples"] > 0
    assert body["report_url"].startswith("/api/factors/reports/")
    assert client.get(body["report_url"]).status_code == 200


def test_evaluate_series_chart_payload(client):
    """评价图表数据包：IC 序列 / 分层净值 / 分组柱 / 衰减 / 分年度。"""
    r = client.post("/api/factors/evaluate/series", json={
        "factor": "MA20", "formula": "MA20", "n_groups": 5, "start": "2024-06-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["ic"]["dates"] and len(body["ic"]["dates"]) == len(body["ic"]["ic"])
    assert len(body["ic"]["cum_ic"]) == len(body["ic"]["ic"])
    q = body["quantile"]
    assert set(q["curves"]) >= {f"q{i}" for i in range(1, 6)} | {"long_short"}
    assert len(q["dates"]) == len(q["curves"]["q1"])
    assert len(q["groups"]) == 5
    assert body["decay"]["horizons"] == [1, 5, 10, 20]
    assert all(v is None or isinstance(v, (int, float)) for v in body["decay"]["ic"])
    assert isinstance(body["ic_by_year"], list)


# ---------- backtests ----------

def test_backtest_run_list_detail_compare(client):
    r1 = client.post("/api/backtests/run", json={
        "formula": "pct_change_20", "top_n": 3, "start": "2026-01-01"})
    r2 = client.post("/api/backtests/run", json={
        "formula": "pct_change_5", "top_n": 2, "start": "2026-01-01"})
    assert r1.status_code == 200 and r2.status_code == 200
    a, b = r1.json()["run_id"], r2.json()["run_id"]

    lst = client.get("/api/backtests").json()
    assert {a, b} <= {x["run_id"] for x in lst}

    detail = client.get(f"/api/backtests/{a}")
    assert detail.status_code == 200

    cmp_ = client.get("/api/backtests/compare", params={"ids": f"{a},{b}"})
    assert cmp_.status_code == 200
    body = cmp_.json()
    assert len(body["series"]) == 2 and len(body["dates"]) > 10

    # 参数校验
    assert client.get("/api/backtests/compare", params={"ids": a}).status_code == 422
    assert client.get("/api/backtests/compare",
                      params={"ids": f"{a},ghost"}).status_code == 404


def test_jq_run_code_attribution_holdings(client):
    """聚宽代码回测全链路：run-code → code → holdings → attribution。"""
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_monthly(rebal, monthday=1, time="open")

def rebal(context):
    order_target_value("600519.SH", context.portfolio.total_value * 0.5)
    order_target_value("000001.SZ", context.portfolio.total_value * 0.3)
'''
    r = client.post("/api/backtests/run-code", json={
        "code": code, "start": "2026-01-01", "initial_cash": 1_000_000})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert r.json()["n_nav_points"] > 10

    # 策略代码可取回
    c = client.get(f"/api/backtests/{rid}/code").json()
    assert "order_target_value" in c["code"]

    # 每日持仓：日期索引 + 单日明细
    h = client.get(f"/api/backtests/{rid}/holdings").json()
    assert len(h["dates"]) > 10
    last_day = h["dates"][-1]["date"]
    hd = client.get(f"/api/backtests/{rid}/holdings", params={"day": last_day}).json()
    assert hd["positions"] and sum(p["weight"] for p in hd["positions"]) <= 1.0

    # 归因：个股贡献 + Brinson + 风险指标
    att = client.get(f"/api/backtests/{rid}/attribution").json()
    assert att["stock_contribution"]["n_stocks"] >= 1
    assert att["brinson"]["groups"]
    assert "alpha_annual" in att["risk"]

    # 语法错误 → 422 带信息
    bad = client.post("/api/backtests/run-code", json={
        "code": "def initialize(context\n  pass", "start": "2026-01-01"})
    assert bad.status_code == 422


# ---------- sync：定时同步 ----------

def test_sync_jobs_crud_and_run(client):
    lst = client.get("/api/sync/jobs").json()
    assert {"close", "evening", "daily", "adj"} <= {j["sync_id"] for j in lst}

    r = client.post("/api/sync/jobs", json={
        "sync_id": "api_test", "name": "API 测试", "kind": "collect",
        "schedule_time": "12:00", "weekdays": "5", "params": {}})
    assert r.status_code == 200

    # 立即执行（demo 离线）
    run = client.post("/api/sync/run", json={"sync_id": "close", "demo": True})
    assert run.status_code == 200
    assert run.json()["status"] in ("ok", "partial")

    hist = client.get("/api/sync/history").json()
    assert any(h["sync_id"] == "close" for h in hist)

    tog = client.post("/api/sync/jobs/api_test/toggle", json={"enabled": False})
    assert tog.status_code == 200 and tog.json()["enabled"] is False
    assert client.delete("/api/sync/jobs/api_test").status_code == 200
    assert client.post("/api/sync/run", json={}).status_code == 422


def test_sync_coverage(client):
    cov = client.get("/api/sync/coverage").json()
    assert "lake" in cov and "reference" in cov and "market_tables" in cov
    assert cov["lake"]["rows"] > 0                     # 拷贝来的 demo 湖非空
    assert any(t["table"] == "security" for t in cov["reference"])
    assert any(t["table"] == "index_daily" for t in cov["market_tables"])


def test_market_index_endpoint(client):
    """指数端点：demo 采集一轮后应有数据。"""
    client.post("/api/market/collect", json={"demo": True})
    r = client.get("/api/market/index")
    assert r.status_code == 200
    body = r.json()
    if body:
        row = body[0]
        assert {"symbol", "name", "close", "chg", "dates", "closes"} <= set(row)


# ---------- market 健康度 ----------

def test_market_collect_and_status(client):
    r = client.post("/api/market/collect", json={"demo": True})
    assert r.status_code == 200
    status = client.get("/api/market/collect-status").json()
    assert "jobs" in status or "collectors" in status or isinstance(status, list)


def test_market_breadth(client):
    r = client.get("/api/market/breadth", params={"days": 20})
    assert r.status_code == 200
    body = r.json()
    assert body["latest"] and body["latest"]["n"] > 0
    assert 0 <= body["latest"]["up_ratio"] <= 1
    assert len(body["history"]) <= 20
    assert {"trade_date", "up", "down", "med_chg"} <= set(body["history"][0])


def test_market_batch_aggregate(client):
    r = client.get("/api/market/batch",
                   params={"symbols": "600519,510300.SH,159915", "days": 30})
    assert r.status_code == 200
    body = r.json()
    assert len(body["series"]) == 3
    assert body["summary"]["n"] == 3
    assert len(body["equal_weight_nav"]) == len(body["dates"])
    assert body["equal_weight_nav"][0] == 1.0
    # 裸码 / 带后缀混用、超量
    assert client.get("/api/market/batch",
                      params={"symbols": ",".join(["600519"] * 21)}).status_code == 422


# ---------- WebSocket ----------

def test_ws_job_not_found(client):
    with client.websocket_connect("/ws/jobs/ghost-job") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "not_found" and msg["done"] is True
