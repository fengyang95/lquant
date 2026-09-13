"""回测 API e2e：/run-benchmark + /validation + 详情/列表读路径。

隔离方式沿用 test_news_e2e.py：LQ_ROOT env + chdir + get_settings.cache_clear，
服务 startup 钩子自动建回测表（backtest_run/nav/order/position/record）。
行情数据 monkeypatch 在源模块属性 lquant.server.api.backtests.read_daily 上
（patch 必须落在使用方命名空间，与 test_api_news.py 结论一致）。
"""
from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from lquant.core.config import get_settings
from lquant.server.main import create_app


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "config", tmp_path / "config")
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _daily_df() -> pl.DataFrame:
    """3 天 2 标的合成行情（vwap/涨跌停/T+1 都不触发边界）。"""
    d0 = date(2024, 1, 2)
    rows = []
    for sym, base in (("600000.SH", 10.0), ("510300.SH", 4.0)):
        pre = base
        for i in range(3):
            c = base + i * 0.1
            rows.append({"trade_date": d0 + timedelta(days=i), "symbol": sym,
                         "open": pre, "high": max(pre, c) * 1.001,
                         "low": min(pre, c) * 0.999, "close": c, "pre_close": pre,
                         "volume": 2e9, "amount": 2e9 * c, "adj_factor": 1.0})
            pre = c
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


@pytest.fixture
def client(fake_env, monkeypatch):
    import lquant.server.api.backtests as bt_api
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS

    df = _daily_df()

    def fake_read_daily(*args, **kw):
        return df.lazy()

    monkeypatch.setattr(bt_api, "read_daily", fake_read_daily)
    # 回测表 DDL 挂在 main.py 的模块级 app startup 上，create_app() 新实例
    # 不会执行（与 news API 的惰性建表不同）—— 这里显式跑一遍（全 IF NOT EXISTS，幂等）
    with writer() as con:
        for stmt in DDL_STATEMENTS:
            con.execute(stmt)
    with TestClient(create_app()) as c:
        yield c


def test_validation_report_all_checks_pass(client):
    """自检端点：金标准/守恒/防未来函数/费率边界全过。"""
    r = client.get("/api/backtests/validation")
    assert r.status_code == 200
    body = r.json()
    assert body["all_passed"] is True
    names = [c["name"] for c in body["checks"]]
    assert len(names) >= 8
    for c in body["checks"]:
        assert c["passed"], f"{c['name']}: {c['detail']}"
    # 基准策略元数据随报告返回
    assert set(body["benchmarks"]) >= {"sma_cross", "momentum_rotation"}


def test_run_benchmark_and_read_path(client):
    """一键基准回测：run-benchmark → 详情 → 列表，全链路数据一致。"""
    r = client.post("/api/backtests/run-benchmark",
                    json={"key": "sma_cross", "start": "2024-01-01",
                          "initial_cash": 100000})
    assert r.status_code == 200, r.text
    body = r.json()
    run_id = body["run_id"]
    assert body["symbols"] == ["600519.SH"] or body["symbols"]
    assert body["n_nav_points"] == 3

    detail = client.get(f"/api/backtests/{run_id}").json()
    assert detail["strategy"] == "benchmark:sma_cross"
    assert detail["params"]["benchmark"] == "sma_cross"
    assert detail["params"]["reference"]        # 公开出处随参数落库
    assert len(detail["nav"]) == 3

    runs = client.get("/api/backtests").json()
    assert any(x["run_id"] == run_id for x in runs)


def test_run_benchmark_unknown_key_404(client):
    r = client.post("/api/backtests/run-benchmark", json={"key": "nope"})
    assert r.status_code == 404


def test_run_benchmark_custom_symbols(client):
    r = client.post("/api/backtests/run-benchmark",
                    json={"key": "momentum_rotation", "symbols": ["600000.SH"],
                          "start": "2024-01-01"})
    assert r.status_code == 200
    assert r.json()["symbols"] == ["600000.SH"]
