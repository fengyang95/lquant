"""sweep HTTP 端点的接线测试：入队 → 轮询 → 拿网格。

用本地降级队列（无 Redis），并把 job 体 monkeypatch 成假计算 ——
只验证「POST 入队、GET 按 id 轮询、结果原样返回」这条链，
不碰 DB / 存储，离线可跑。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lquant.server.main import create_app

_GRID = [
    {"value": 1.0, "total_return": 0.12, "annual_return": 0.3,
     "sharpe": 1.2, "max_drawdown": -0.1, "n_trades": 12, "turnover": 0.8,
     "rebalance": "monthly", "param": "top_n"},
    {"value": 2.0, "total_return": 0.15, "annual_return": 0.35,
     "sharpe": 1.4, "max_drawdown": -0.08, "n_trades": 20, "turnover": 0.9,
     "rebalance": "monthly", "param": "top_n"},
]


@pytest.fixture()
def client(monkeypatch):
    # monkeypatch job 体：不读 DB，返回固定网格
    from lquant.server.api import backtests as bt

    monkeypatch.setattr(bt, "_run_sweep_job",
                        lambda *a, **k: [dict(r) for r in _GRID])
    app = create_app()
    return TestClient(app)


def test_sweep_post_then_poll(client):
    r = client.post("/api/backtests/sweep", json={"formula": "pct_change_5",
                                                  "param": "top_n",
                                                  "values": [1, 2],
                                                  "rebalance": "monthly"})
    assert r.status_code == 200, r.text
    body = r.json()
    sid = body["sweep_id"]
    assert body["status"] == "queued"
    assert body["n_points"] == 2

    # 轮询直到 done（本地降级是后台线程，最多等 5s）
    got = None
    for _ in range(50):
        rr = client.get(f"/api/backtests/sweep/{sid}")
        assert rr.status_code == 200, rr.text
        got = rr.json()
        if got["grid"] is not None:
            break
        time.sleep(0.1)
    assert got["grid"] is not None
    assert len(got["grid"]) == 2
    assert got["grid"][0]["total_return"] == pytest.approx(0.12)
    # 列随 job 体透传，value 是 float
    assert isinstance(got["grid"][1]["value"], float)


def test_sweep_get_unknown_id_404(client):
    assert client.get("/api/backtests/sweep/nope").status_code == 404


def test_sweep_rejects_bad_param(client):
    r = client.post("/api/backtests/sweep", json={"param": "top_n_hack",
                                                  "values": [1]})
    assert r.status_code == 422

def test_sweep_post_reports_engine(client):
    """端点回传实际选择：小网格 event、大网格 vector、显式 vector 服从请求。"""
    from lquant.backtest.sweep import VECTOR_AUTO_MIN_POINTS

    def _post(**kw):
        base = {"formula": "pct_change_5", "param": "top_n", "rebalance": "monthly"}
        return client.post("/api/backtests/sweep", json={**base, **kw}).json()

    big = list(range(1, VECTOR_AUTO_MIN_POINTS + 1))
    assert _post(values=[1, 2])["engine"] == "event"
    assert _post(values=big)["engine"] == "vector"
    assert _post(values=[1, 2], engine="vector")["engine"] == "vector"
    assert _post(values=big, engine="event")["engine"] == "event"


def _tiny_panel():
    """`_run_sweep_job` 的最小行情：够 _compute_factor 派生因子列即可。"""
    from datetime import date, timedelta

    import polars as pl

    rows = []
    d0 = date(2026, 1, 5)
    for s in ("600000", "000001", "300750"):
        for i in range(8):
            rows.append({"trade_date": d0 + timedelta(days=i), "symbol": s,
                         "close": 10.0 + i, "open": 10.0 + i, "high": 11.0 + i,
                         "low": 9.0 + i, "pre_close": 9.5 + i})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def _fake_grid():
    import polars as pl

    return pl.DataFrame({
        "value": [1, 2], "total_return": [0.1, 0.2], "annual_return": [0.2, 0.4],
        "sharpe": [1.0, 2.0], "max_drawdown": [-0.1, -0.05],
        "n_trades": [3, 4], "turnover": [0.1, 0.2],
    })


def test_sweep_job_selects_vectorized_engine(monkeypatch):
    """`_run_sweep_job` 按 engine 真正走到向量化/事件两条实现路径。"""
    from lquant.backtest import sweep as sweep_mod
    from lquant.server.api import backtests as bt

    seen: list[str] = []
    monkeypatch.setattr(sweep_mod, "run_sweep_vectorized",
                        lambda *a, **k: seen.append("vector") or _fake_grid())
    monkeypatch.setattr(sweep_mod, "run_sweep",
                        lambda *a, **k: seen.append("event") or _fake_grid())
    df = _tiny_panel()
    monkeypatch.setattr(bt, "read_daily", lambda **kw: df.lazy())

    def _run(engine, values):
        cfg = {"formula": "pct_change_5", "rebalance": "monthly",
               "initial_cash": 1_000_000.0, "start": "2026-01-01",
               "top_n": 5, "engine": engine}
        return bt._run_sweep_job("pct_change_5", "top_n", values, cfg)

    assert _run("vector", [1, 2])[0]["engine"] == "vector"
    assert seen[-1] == "vector"
    assert _run("auto", [1, 2])[0]["engine"] == "event"
    assert seen[-1] == "event"
    big = list(range(1, sweep_mod.VECTOR_AUTO_MIN_POINTS + 1))
    assert _run("auto", big)[0]["engine"] == "vector"
    assert seen[-1] == "vector"
