"""新增端点的 API 契约测试：指标注册表 / 退出策略 / 基本面分位。

全程离线：tmp 目录 + DDL 建表 + 合成 PIT 财务，不依赖本地真实数据。
"""
from __future__ import annotations

import os
from datetime import date

import polars as pl
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")

PUB = date(2026, 3, 20)
STAT = date(2025, 12, 31)

# 覆盖 metrics 目录里的全部 item（让覆盖率断言有意义）
ITEMS = ("profit.roeAvg", "profit.npMargin", "profit.gpMargin", "dupont.dupontNitogr",
         "cashflow.CFOToNP", "cashflow.CFOToOR", "cashflow.CFOToGr",
         "operation.NRTurnDays", "operation.INVTurnDays", "operation.AssetTurnRatio",
         "balance.currentRatio", "balance.quickRatio", "balance.liabilityToAsset",
         "cashflow.ebitToInterest",
         "valuation.pe_ttm", "valuation.pb", "valuation.dividend_yield")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("port_api")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        _seed_financial(con)
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


def _seed_financial(con) -> None:
    """两个行业 × 6 只票 × 17 个指标，公告日 2026-03-20。"""
    rows = []
    industries = []
    for i in range(12):
        sym = f"6000{i:02d}.SH"
        ind = "白酒" if i < 6 else "银行"
        industries.append((sym, ind))
        for k, item in enumerate(ITEMS):
            lower_better = item in ("operation.NRTurnDays", "operation.INVTurnDays",
                                    "balance.liabilityToAsset", "valuation.pe_ttm",
                                    "valuation.pb")
            base = 40.0 - i * 2 if lower_better else 10.0 + i * 2
            rows.append((sym, STAT, PUB, "2025Q4", item, base + k, "baostock"))
    con.executemany(
        "INSERT INTO financial_pit (symbol, stat_date, pub_date, report_type, item,"
        " value, source) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany(
        "INSERT INTO industry_classify (symbol, std, code, name, std_date, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(sym, "sw", ind, ind, date(2024, 1, 1), "test") for sym, ind in industries])


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------- 指标 ----------

def test_indicator_registry(client):
    r = client.get("/api/data/indicators/registry")
    assert r.status_code == 200, r.text
    body = r.json()
    names = [i["name"] for i in body["indicators"]]
    assert {"ma", "macd", "rsi", "boll", "kdj", "tiandao"} <= set(names)
    assert body["default"] == ["ma", "macd", "rsi", "boll"]
    for meta in body["indicators"]:
        assert {"name", "label", "category", "pane", "min_window", "outputs"} <= set(meta)
        assert meta["category"] in ("trend", "oscillator", "volume", "pattern", "channel")
        assert meta["pane"] in ("price", "sub", "volume")


def test_indicator_unknown_name_is_422(client):
    r = client.get("/api/data/indicators?symbol=600519.SH&names=ma,nope")
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]


def test_indicator_empty_names_is_422(client):
    r = client.get("/api/data/indicators?symbol=600519.SH&names=,")
    assert r.status_code == 422


def test_indicator_empty_lake_returns_empty_list(client):
    """空湖是常态：返回 [] 而不是 500。"""
    r = client.get("/api/data/indicators?symbol=600519.SH&limit=5&names=kdj")
    assert r.status_code == 200
    assert r.json() == []


# ---------- 退出策略 ----------

def test_exit_strategy_registry(client):
    r = client.get("/api/backtests/exit-strategies")
    assert r.status_code == 200, r.text
    names = [s["name"] for s in r.json()["strategies"]]
    assert set(names) == {"simple", "tiered", "pressure"}
    assert all(s["label"] for s in r.json()["strategies"])


def test_backtest_rejects_unknown_exit_strategy(client, monkeypatch):
    from lquant.server.api import backtests as bt

    monkeypatch.setattr(bt, "read_daily",
                        lambda **kw: pl.DataFrame({
                            "symbol": ["600519.SH"] * 30,
                            "trade_date": [date(2026, 1, 1)] * 30,
                            "close": [100.0 + i for i in range(30)],
                        }).lazy())
    r = client.post("/api/backtests/run", json={"exit_strategy": "no_such_exit"})
    assert r.status_code == 422
    assert "no_such_exit" in r.json()["detail"]


def test_backtest_rejects_bad_exit_params(client, monkeypatch):
    from lquant.server.api import backtests as bt

    monkeypatch.setattr(bt, "read_daily",
                        lambda **kw: pl.DataFrame({
                            "symbol": ["600519.SH"] * 30,
                            "trade_date": [date(2026, 1, 1)] * 30,
                            "close": [100.0 + i for i in range(30)],
                        }).lazy())
    r = client.post("/api/backtests/run",
                    json={"exit_strategy": "simple", "exit_params": {"stop_loss_pct": -1}})
    assert r.status_code == 422
    assert "退出策略参数非法" in r.json()["detail"]


def test_backtest_runs_with_exit_strategy(client, monkeypatch):
    from lquant.server.api import backtests as bt

    n = 120
    px = [100.0 + i * 0.5 for i in range(n)]
    monkeypatch.setattr(bt, "read_daily",
                        lambda **kw: pl.DataFrame({
                            "symbol": ["600519.SH"] * n,
                            "trade_date": [date(2026, 1, 1)] * n,
                            "open": px, "high": [p * 1.01 for p in px],
                            "low": [p * 0.99 for p in px], "close": px,
                            "pre_close": [px[0], *px[:-1]],
                            "volume": [1e7] * n,
                        }).lazy())
    r = client.post("/api/backtests/run",
                    json={"exit_strategy": "tiered", "top_n": 1, "rebalance": "daily",
                          "initial_cash": 1_000_000})
    assert r.status_code == 200, r.text
    assert r.json()["run_id"]


# ---------- 基本面 ----------

def test_fundamental_metrics(client):
    r = client.get("/api/fundamental/metrics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert sum(m["weight"] for m in body["modules"]) == pytest.approx(100.0)
    assert len(body["metrics"]) == 17
    assert all({"item", "label", "module", "max_score", "direction"} <= set(m)
               for m in body["metrics"])


def test_fundamental_percentiles(client):
    r = client.get("/api/fundamental/percentiles?asof=2026-04-01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["rows"], "应算出分位"
    assert {"industry", "item", "p25", "p50", "p75", "n"} <= set(body["rows"][0])


def test_fundamental_percentiles_before_pub_date_is_empty(client):
    """公告日之前查不到 —— 前视偏差防线在 API 层同样生效。"""
    r = client.get("/api/fundamental/percentiles?asof=2026-01-10")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_fundamental_score_single_symbol(client):
    r = client.get("/api/fundamental/score?symbol=600000.SH&asof=2026-04-01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["symbol"] == "600000.SH"
    assert body["score"]["n_metrics"] == 17
    assert body["score"]["coverage"] == pytest.approx(1.0)   # 17 个 item 全部灌入
    assert 0.0 <= body["score"]["normalized_score"] <= 100.0
    assert body["score"]["rating"] in ("优秀", "良好", "一般", "较差")
    assert body["items"], "应返回逐指标明细"


def test_fundamental_score_unknown_symbol(client):
    r = client.get("/api/fundamental/score?symbol=999999.SZ&asof=2026-04-01")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_fundamental_score_bad_symbol_is_422(client):
    r = client.get("/api/fundamental/score?symbol=not-a-code")
    assert r.status_code == 422


def test_fundamental_scores_ranking_and_filter(client):
    r = client.post("/api/fundamental/scores",
                    json={"asof": "2026-04-01", "limit": 5, "min_coverage": 0.5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert 0 < body["n_scored"] <= 5
    scores = [row["normalized_score"] for row in body["rows"]]
    assert scores == sorted(scores, reverse=True), "必须按归一化分数降序"


def test_fundamental_scores_symbol_filter(client):
    r = client.post("/api/fundamental/scores",
                    json={"asof": "2026-04-01", "symbols": ["600000.SH", "600001.SH"]})
    assert r.status_code == 200
    got = {row["symbol"] for row in r.json()["rows"]}
    assert got <= {"600000.SH", "600001.SH"}


def test_fundamental_scores_before_pub_date_unavailable(client):
    r = client.post("/api/fundamental/scores", json={"asof": "2026-01-10"})
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_fundamental_reconcile_pass_and_fail(client):
    ok = client.post("/api/fundamental/reconcile", json={
        "symbol": "600519.SH", "net_income": 100.0, "delta_retained": 102.0,
        "cashflow_net_change": 50.0, "balance_cash_change": 50.0,
        "deducted_net_income": 97.0}).json()
    assert ok["passed"] is True and ok["failed"] == []

    bad = client.post("/api/fundamental/reconcile", json={
        "symbol": "600519.SH", "net_income": 100.0, "delta_retained": 190.0}).json()
    assert bad["passed"] is False
    assert "retained_earnings" in bad["failed"]


def test_fundamental_reconcile_no_data_is_not_pass(client):
    r = client.post("/api/fundamental/reconcile", json={"symbol": "600519.SH"})
    assert r.status_code == 200
    assert r.json()["passed"] is False
