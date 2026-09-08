"""技术指标数值正确性测试（M3）：MA / EMA / MACD（国内口径）/ RSI（Wilder）/ BOLL。"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.factors.indicators import add_all, add_boll, add_macd, add_ma, add_rsi


def make_series(n: int = 60, seed: int = 11) -> pl.DataFrame:
    from datetime import date, timedelta

    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    return pl.DataFrame({
        "trade_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "close": close,
    })


def test_ma_matches_manual_mean():
    df = add_ma(make_series(), periods=(5,))
    m = df["ma5"].to_numpy()
    c = df["close"].to_numpy()
    assert np.isnan(m[:4]).all()                       # 窗口不足为 NaN
    for i in range(4, len(c)):
        assert m[i] == pytest.approx(c[i - 4:i + 1].mean(), abs=1e-9)


def test_macd_hist_domestic_convention():
    """国内软件口径 HIST = 2 × (DIF − DEA)。"""
    df = add_macd(make_series())
    d = df.to_dicts()[-1]
    assert d["macd_hist"] == pytest.approx(2 * (d["macd_dif"] - d["macd_dea"]), abs=1e-9)


def test_rsi_bounded_and_extremes():
    df = add_rsi(make_series())
    r = df["rsi14"].drop_nulls()
    assert (r >= 0).all() and (r <= 100).all()
    # 单调上涨序列 RSI 趋近 100，单调下跌趋近 0
    up = pl.DataFrame({"trade_date": list(range(30)),
                       "close": np.arange(30, dtype=float)})
    dn = pl.DataFrame({"trade_date": list(range(30)),
                       "close": np.arange(30, 0, -1, dtype=float)})
    assert add_rsi(up)["rsi14"][-1] > 95
    assert add_rsi(dn)["rsi14"][-1] < 5


def test_boll_bands_around_ma():
    df = add_boll(make_series())
    last = df.to_dicts()[-1]
    c = df["close"].to_numpy()
    assert last["boll_mid"] == pytest.approx(c[-20:].mean(), abs=1e-9)
    assert last["boll_upper"] > last["boll_mid"] > last["boll_lower"]


def test_add_all_columns_present():
    df = add_all(make_series())
    for col in ("ma5", "ma20", "ema12", "macd_dif", "macd_dea", "macd_hist",
                "rsi14", "boll_upper", "boll_mid", "boll_lower"):
        assert col in df.columns, col
    assert df["ma5"].null_count() == df["ma20"].null_count() - 15  # 窗口差
