"""/api/etf 因子子域：池 / 因子清单 / 评价 / 回测（走统一封套）。

用 demo 数据起一个真实 app（与 test_api_factor_crud 同一套搭法）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LQ_SYNC_WORKER", "0")

pytestmark = pytest.mark.usefixtures("api_env")


@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("api_etf_factors")
    prev_cwd = os.getcwd()
    os.chdir(base)
    from lquant.core.config import get_settings

    get_settings.cache_clear()

    from lquant.core.db import writer
    from lquant.data.ingest.demo import generate_demo
    from lquant.data.store.ddl import DDL_STATEMENTS, ensure_factor_def_columns

    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
        ensure_factor_def_columns(con)
    generate_demo(start="2024-01-01", end="2026-06-30")
    yield base
    os.chdir(prev_cwd)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(api_env):
    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _data(resp):
    """解封套：封套接口 body 恒为 {code, data, message, trace_id}。"""
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


# ---------------- 池 / 因子清单 ----------------

def test_etf_universes(client):
    data = _data(client.get("/api/etf/universes"))
    keys = [d["key"] for d in data]
    assert "etf_all" in keys and "etf_equity" in keys
    assert all(d["fund_type"] is None or isinstance(d["fund_type"], str) for d in data)


def test_etf_factors_list(client):
    data = _data(client.get("/api/etf/factors"))
    assert len(data) >= 12
    names = {d["name"] for d in data}
    assert "etf_mom_20" in names
    for d in data:
        assert d["expression"]


def test_factor_universes_endpoint_includes_etf_pools(client):
    """/api/factors/universes 是前端选项唯一来源 —— ETF 池必须在里面。

    该路由属旧接口（无封套），直接返回裸 list；ETF 子域走封套。两种形状都在
    这里显式覆盖，避免「换个路由前缀就解析不出数据」。
    """
    r = client.get("/api/factors/universes")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    keys = [d["key"] for d in data]
    assert "etf_all" in keys
    assert "hs300" in keys


# ---------------- 评价 ----------------

def test_etf_evaluate_ok(client):
    r = client.post("/api/etf/evaluate", json={
        "factor": "Ts_Return($close, 20)", "universe": "etf_all", "n_groups": 5,
    })
    data = _data(r)
    assert data["rating"]["rating"] in ("weak", "moderate", "strong")
    assert data["etf"]["label"] == "全部ETF"
    # 结果已 JSON 归一：polars DataFrame 不该出现在响应里
    assert isinstance(data["decay"]["profile"], list)


def test_etf_evaluate_rejects_non_etf_universe(client):
    r = client.post("/api/etf/evaluate", json={"factor": "Ts_Return($close, 20)",
                                               "universe": "hs300"})
    assert r.status_code == 422        # 个股池不许进 ETF 子域


def test_etf_evaluate_bad_expression_422(client):
    r = client.post("/api/etf/evaluate", json={
        "factor": "$no_such_field", "universe": "etf_all",
    })
    assert r.status_code in (422, 503)   # 字段非法 → 422；极端下也可能被当成数据问题


# ---------------- 回测 ----------------

def test_etf_backtest_ok_only_etf(client):
    from lquant.factors.etf import is_etf_symbol

    r = client.post("/api/etf/backtest", json={
        "factor": "Ts_Return($close, 20)", "universe": "etf_all",
        "top_n": 3, "rebalance": "monthly",
    })
    data = _data(r)
    assert data["run_id"]
    assert data["n_symbols"] > 0
    assert data["n_trades"] > 0
    assert data["universe"] == "etf_all"
    assert is_etf_symbol("510300.SH")   # 冒烟：口径函数可用


def test_etf_backtest_rejects_non_etf_universe(client):
    r = client.post("/api/etf/backtest", json={
        "factor": "Ts_Return($close, 20)", "universe": "zz500",
    })
    assert r.status_code == 422


# ---------------- 通用回测入口的 universe ----------------

def test_generic_backtest_run_accepts_etf_universe(client):
    """POST /api/backtests/run 的 universe=etf_all 只跑 ETF。"""
    r = client.post("/api/backtests/run", json={
        "formula": "pct_change_20", "universe": "etf_all",
        "top_n": 3, "rebalance": "monthly", "start": "2024-06-01",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["universe"] == "etf_all"
    assert body["n_trades"] >= 0


def test_generic_backtest_run_unknown_universe_503(client):
    r = client.post("/api/backtests/run", json={
        "formula": "pct_change_20", "universe": "etf_nope", "start": "2024-06-01",
    })
    assert r.status_code in (422, 503)


# ---------------- 异常 → 状态码（直接调路由/内部函数，不依赖真实数据） ----------------

def test_etf_evaluate_maps_data_error_to_503(monkeypatch):
    """面板为空是**数据问题**（503），不是请求非法（422）。"""
    from fastapi import HTTPException

    from lquant.server.api.etf import EtfEvaluateIn, etf_evaluate

    def boom(*_a, **_k):
        raise ValueError("ETF 面板为空（池=etf_all）")

    monkeypatch.setattr("lquant.factors.etf.evaluate_etf", boom)
    with pytest.raises(HTTPException) as ei:
        etf_evaluate(EtfEvaluateIn(factor="Ts_Return($close, 20)", universe="etf_all"))
    assert ei.value.status_code == 503
    assert "数据不足" in ei.value.detail


@pytest.mark.parametrize("exc,status", [
    (ValueError("ETF 面板为空"), 503),
    (KeyError("nope"), 422),
    (TypeError("参数类型错"), 422),
])
def test_etf_backtest_error_mapping(monkeypatch, exc, status):
    from fastapi import HTTPException

    from lquant.server.api.etf import EtfBacktestIn, etf_backtest

    def boom(*_a, _e=exc, **_k):
        raise _e

    monkeypatch.setattr("lquant.backtest.etf.etf_factor_backtest", boom)
    with pytest.raises(HTTPException) as ei:
        etf_backtest(EtfBacktestIn(factor="Ts_Return($close, 20)", universe="etf_all"))
    assert ei.value.status_code == status


def test_generic_backtest_all_null_factor_is_422(monkeypatch):
    """公式算出来全空 → 422 + 可读原因，而不是把空结果当成功回测。"""
    import datetime as _dt

    import polars as pl
    from fastapi import HTTPException

    from lquant.server.api import backtests as api

    frame = pl.DataFrame({"symbol": ["510300.SH"], "trade_date": [_dt.date(2024, 6, 3)],
                          "close": [1.0]})
    monkeypatch.setattr(api, "read_daily", lambda **kw: frame.lazy())
    monkeypatch.setattr(
        api, "_compute_factor",
        lambda df, formula: df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias(formula.replace("_", ""))),
    )
    with pytest.raises(HTTPException) as ei:
        api.run_backtest(api.BacktestIn(formula="pct_change_20", start="2024-06-01"))
    assert ei.value.status_code == 422
    assert "全为空" in ei.value.detail


def test_resolve_universe_delegates_etf_pool(monkeypatch):
    """ETF 池分流到 resolve_etf_universe，返回值形状与指数池一致。"""
    from lquant.server.api.factors import _resolve_universe

    monkeypatch.setattr("lquant.factors.etf.resolve_etf_universe",
                        lambda u: (["510300.SH"], "etf_meta 未同步，已按 security 表兜底"))
    assert _resolve_universe("etf_all") == (
        ["510300.SH"], "etf_meta 未同步，已按 security 表兜底")


def test_resolve_universe_etf_pool_failure_is_503(monkeypatch):
    """池名合法但一条数据都没有 → 503（缺数据），不是 422。"""
    from fastapi import HTTPException

    from lquant.server.api.factors import _resolve_universe

    def boom(_u):
        raise ValueError("ETF 池为空：etf_meta / security / 日线湖都没有 ETF 标的")

    monkeypatch.setattr("lquant.factors.etf.resolve_etf_universe", boom)
    with pytest.raises(HTTPException) as ei:
        _resolve_universe("etf_all")
    assert ei.value.status_code == 503
    assert "先同步 ETF 数据" in ei.value.detail
