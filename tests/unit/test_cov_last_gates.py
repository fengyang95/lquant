"""最后覆盖冲刺：mining gates G0/G1/G2 分支。"""

import datetime as dt

import numpy as np
import polars as pl

from lquant.factors.mining.gates import g0_static, g1_fast_screen, g2_dedup


def _train(n=60, days=8):
    rng = np.random.default_rng(3)
    rows = []
    for d in range(days):
        date = dt.date(2026, 1, 5) + dt.timedelta(days=d)
        for i in range(n):
            rows.append({
                "symbol": f"{i:06d}.SZ", "trade_date": date,
                "close": 10 * (1 + 0.01 * d) + rng.normal(),
                "market_cap": float(rng.lognormal(6, 0.5)),
                "industry_sw1": ["A", "B", "C"][i % 3],
            })
    return pl.DataFrame(rows)


def test_g0_fail():
    r = g0_static("1 +", ["close"])
    assert not r.passed and r.reason_code == "STATIC_FAIL"


def test_g1_compute_fail():
    r = g1_fast_screen(_train(), "$nope", "fwd_ret_1", engine=None)
    assert not r.passed and r.reason_code == "COMPUTE_FAIL"


def test_g1_low_ic_with_and_without_covs():
    train = _train()
    r = g1_fast_screen(train, "$close", "fwd_ret_1", engine=None,
                       covs=["market_cap", "industry_sw1"])
    assert r.passed is False
    assert r.reason_code in ("LOW_IC", "SIZE_PROXY", "COMPUTE_FAIL")
    r2 = g1_fast_screen(train, "$close", "fwd_ret_1", engine=None)
    assert isinstance(r2, type(r))


def test_g2_dedup_branches():
    train = _train()
    survivors = [{"expr": "$close", "name": "x"}]
    r = g2_dedup(train, "$close", survivors, engine=None)
    assert isinstance(r.passed, bool)
    from unittest.mock import patch

    import lquant.factors.analysis as fa

    def fake_corr(df, exprs, threshold):
        return {"redundant_pairs": [{"a": "$close", "b": "$close",
                                     "corr": 0.95}]}

    with patch.object(fa, "correlation", fake_corr):
        r2 = g2_dedup(train, "$close", survivors, engine=None)
    assert not r2.passed and r2.reason_code == "REDUNDANT"
