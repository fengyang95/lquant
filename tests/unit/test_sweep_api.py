"""sweep HTTP 端点的接线测试：入队 → 轮询 → 拿网格。

用本地降级队列（无 Redis），并把 job 体 monkeypatch 成假计算 ——
只验证「POST 入队、GET 按 id 轮询、结果原样返回」这条链，
不碰 DB / 存储，离线可跑。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lquant.core.config import get_settings
from lquant.server import api as api_pkg
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