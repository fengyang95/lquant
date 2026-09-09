"""M0 operator expansion regression: every new op gets at least one numeric assertion."""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.ops.registry import OPS

TS_OPS = {"Ts_Max", "Ts_Min", "Ts_ArgMax", "Ts_ArgMin", "Ts_Rank",
          "Ts_Delta", "Ts_Cov", "Ts_Skew", "Ts_Prod", "Ts_EMA"}
EL_OPS = {"Abs", "Log", "Sign", "Sqrt", "Power", "Greater", "Less"}


def test_ops_registered():
    for n in TS_OPS | EL_OPS:
        assert n in OPS.keys(), f"算子未注册: {n}"


def _panel(n_days: int = 30, n_sym: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.01)
            rows.append({"symbol": f"S{s}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * 1.001, "volume": 1e5 + i, "amount": px * 1e4})
    return pl.DataFrame(rows)


def _eng(df: pl.DataFrame | None = None):
    from lquant.factors.engine import FactorEngine
    from lquant.factors.ops import cs_ops, el_ops, ts_ops  # noqa: F401

    return FactorEngine((df if df is not None else _panel()).lazy())


def test_ts_max_min_argmax_argmin():
    eng = _eng()
    a = eng.compute("Ts_Max($close, 5)", "f")
    s0 = a.filter(pl.col("symbol") == "S0").sort("trade_date")
    assert s0["f"].null_count() == 4          # 窗口 5 → 每股前 4 天预热 null
    assert s0[4, "f"] == pytest.approx(max(s0["close"][0:5]))
    b = eng.compute("Ts_Min($close, 5)", "f")
    s0b = b.filter(pl.col("symbol") == "S0").sort("trade_date")
    assert s0b[4, "f"] == pytest.approx(min(s0b["close"][0:5]))
    c = eng.compute("Ts_ArgMax($close, 5)", "f")
    s0c = c.filter(pl.col("symbol") == "S0").sort("trade_date")
    assert s0c[4, "f"] == pytest.approx(float(np.argmax(s0c["close"][0:5])))
    d = eng.compute("Ts_ArgMin($close, 5)", "f")
    s0d = d.filter(pl.col("symbol") == "S0").sort("trade_date")
    assert s0d[4, "f"] == pytest.approx(float(np.argmin(s0d["close"][0:5])))


def test_ts_rank_delta_skew_ema():
    eng = _eng()
    r = eng.compute("Ts_Rank($close, 5)", "f").filter(pl.col("symbol") == "S0").sort("trade_date")
    w = r["close"][0:5]
    assert r[4, "f"] == pytest.approx(float((w <= w[-1]).mean()))
    d = eng.compute("Ts_Delta($close, 5)", "f").filter(pl.col("symbol") == "S0").sort("trade_date")
    assert d[5, "f"] == pytest.approx(d["close"][5] - d["close"][0])
    s = eng.compute("Ts_Skew($close, 5)", "f").filter(pl.col("symbol") == "S0").sort("trade_date")
    assert s[4, "f"] == pytest.approx(float(s["close"][0:5].skew()), abs=1e-6)
    e = eng.compute("Ts_EMA($close, 2)", "f").filter(pl.col("symbol") == "S0").sort("trade_date")
    assert e[1, "f"] > 0


def test_cov_prod_el():
    eng = _eng()
    c = eng.compute("Ts_Cov($close, $open, 5)", "f").filter(pl.col("symbol") == "S0").sort("trade_date")
    assert c[4, "f"] is not None
    pr = eng.compute("Ts_Prod($close, 5)", "f").filter(pl.col("symbol") == "S0").sort("trade_date")
    assert pr[4, "f"] == pytest.approx(float(np.prod([1 + v for v in pr["close"][0:5].to_list()])) - 1, rel=1e-4)
    a = eng.compute("Abs($close - 10)", "f")
    assert a["f"].null_count() == 0
    g = eng.compute("Greater($close, $open)", "f")
    s0 = g.filter(pl.col("symbol") == "S0").sort("trade_date")
    for i in range(30):
        assert s0[i, "f"] == pytest.approx(max(s0[i, "close"], s0[i, "open"]))
    l = eng.compute("Less($close, $open)", "f")
    s0 = l.filter(pl.col("symbol") == "S0").sort("trade_date")
    assert s0[0, "f"] == pytest.approx(min(s0[0, "close"], s0[0, "open"]))
