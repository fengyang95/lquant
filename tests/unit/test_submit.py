"""M4c submit 重验回归。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl


def _panel(n_days=120, n_sym=8):
    rng = np.random.default_rng(11)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": f"S{s:02d}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * (1 + rng.normal(0, 0.001)),
                         "volume": float(1e5 + i), "amount": px * 1e4,
                         "turnover_rate": 0.5})
    return pl.DataFrame(rows)


def test_verify_rejects_static_fail():
    from lquant.factors.mining.submit import verify_and_register

    ok, payload = verify_and_register({"name": "bad", "expr": "Ts_Mean($closs,5)"})
    assert not ok and payload["grade"] == "REJECTED"
    assert payload["reason_code"] == "STATIC_FAIL"


def test_verify_grades_claimed(monkeypatch, tmp_path):
    """claimed 与重算值的分级逻辑：A/B 入库，C/D 归档。"""
    import lquant.factors.mining.submit as sub

    def fake_split(df, covs, expr):
        import polars as pl

        s = pl.DataFrame({"ic": [0.05] * 30, "rank_ic": [0.04] * 30})
        return {"train": s, "val": s}

    monkeypatch.setattr(sub, "_panel_with_covs", lambda start=None: (_panel(), []))
    monkeypatch.setattr(sub, "_split_eval", fake_split)
    ok, payload = sub.verify_and_register({
        "name": "t_fine", "expr": "Ts_Mean($close,5)", "agent": "tester",
        "claimed": {"ic_mean": 0.051},
    })
    assert ok and payload["grade"] == "A"
    ok2, p2 = sub.verify_and_register({
        "name": "t_flip", "expr": "Ts_Mean($close,5)",
        "claimed": {"ic_mean": -0.05},
    })
    assert not ok2 and p2["grade"] == "D"
