"""Qlib Alpha158 内置因子数值正确性测试。

对照 qlib 官方公式（qlib/contrib/data/loader.py::Alpha158DL）手算验证。
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.factors.qlib_alpha import WINDOWS, compute, list_builtin, resolve_name


def make_ohlcv(n_days: int = 120, n_syms: int = 3, seed: int = 5) -> pl.DataFrame:
    """确定性 OHLCV 合成数据。"""
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_syms):
        close = 10 * (1 + np.cumsum(rng.normal(0.001, 0.02, n_days)))
        for i in range(n_days):
            c = close[i]
            o = c * (1 + rng.normal(0, 0.004))
            h = max(o, c) * (1 + abs(rng.normal(0, 0.004)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.004)))
            v = float(rng.integers(1e6, 5e6))
            rows.append({"trade_date": i, "symbol": f"S{s}",
                         "open": o, "high": h, "low": l, "close": c,
                         "pre_close": close[i - 1] if i else c,
                         "volume": v, "amount": v * c})
    return pl.DataFrame(rows)


# ---------------------------------------------------------------- 注册表

def test_158_factors_registered():
    lb = list_builtin()
    assert len(lb) == 158
    assert len({x["name"] for x in lb}) == 158            # 无重名
    assert {x["family"] for x in lb} >= {"kbar", "price", "roc", "ma", "corr", "wvma"}


def test_resolve_name():
    assert resolve_name("ma20") == ("MA", 20)
    assert resolve_name("KMID") == ("KBAR", None)
    assert resolve_name("VWAP0") == ("PRICE", 0)
    with pytest.raises(KeyError):
        resolve_name("MA7")                                # 不在窗口集
    with pytest.raises(KeyError):
        resolve_name("NOPE99")


# ---------------------------------------------------------------- 数值对照

def test_ma_and_roc_match_manual():
    df = make_ohlcv(n_syms=1)
    out = compute(df, "MA20")
    c = df["close"].to_numpy()
    expect = np.full(len(c), np.nan)
    expect[19:] = np.convolve(c, np.ones(20) / 20, mode="valid")
    got = out["_factor"].to_numpy()
    np.testing.assert_allclose(got[19:], expect[19:] / c[19:], rtol=1e-10)

    out = compute(df, "ROC5")
    got = out["_factor"].to_numpy()
    np.testing.assert_allclose(got[5:], c[:-5] / c[5:], rtol=1e-10)


def test_kbar_matches_formula():
    df = make_ohlcv(n_syms=1)
    r = compute(df, "KMID").with_columns(
        ((pl.col("close") - pl.col("open")) / pl.col("open")).alias("_exp"))
    np.testing.assert_allclose(r["_factor"].to_numpy(), r["_exp"].to_numpy(), rtol=1e-12)

    r = compute(df, "KSFT2").with_columns(
        ((2 * pl.col("close") - pl.col("high") - pl.col("low"))
         / (pl.col("high") - pl.col("low") + 1e-12)).alias("_exp"))
    np.testing.assert_allclose(r["_factor"].to_numpy(), r["_exp"].to_numpy(), rtol=1e-9)


def test_beta_slope_close_form():
    """BETA5 与逐窗 OLS 斜率一致。"""
    df = make_ohlcv(n_syms=1, n_days=60)
    got = compute(df, "BETA5")["_factor"].to_numpy() * df["close"].to_numpy()
    c = df["close"].to_numpy()
    t = np.arange(5.0)
    tc = t - t.mean()
    for i in range(5, len(c)):
        y = c[i - 4:i + 1]
        expect = (tc * (y - y.mean())).sum() / (tc**2).sum()
        assert got[i] == pytest.approx(expect, rel=1e-8)


def test_rank_percentile_semantics():
    df = pl.DataFrame({
        "trade_date": list(range(9)),
        "symbol": ["S0"] * 9,
        "open": [1.0] * 9, "high": [2.0] * 9, "low": [0.5] * 9,
        "close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0],
        "pre_close": [1.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
        "volume": [100.0] * 9, "amount": [100.0] * 9,
    })
    got = compute(df, "RANK5")["_factor"].to_list()
    # 单调上涨序列里每个窗口末值都是最大 → 恒为 5/5=1
    assert got[4] == pytest.approx(1.0)
    assert got[8] == pytest.approx(1.0)
    # 下跌段验证非满值
    df2 = df.with_columns(pl.col("close").reverse().alias("close"))
    got2 = compute(df2, "RANK5")["_factor"].to_list()
    assert got2[4] == pytest.approx(1 / 5)                 # 窗口内最小


def test_cntp_sumd_consistency():
    """SUMD = SUMP - SUMN（定义恒等式）。"""
    df = make_ohlcv(n_syms=1)
    sump = compute(df, "SUMP10")["_factor"].to_numpy()
    sumn = compute(df, "SUMN10")["_factor"].to_numpy()
    sumd = compute(df, "SUMD10")["_factor"].to_numpy()
    m = ~np.isnan(sumd)
    np.testing.assert_allclose(sumd[m], (sump - sumn)[m], atol=1e-10)


def test_multi_symbol_grouping():
    """多标的分组：每个 symbol 的 warmup 期独立为 null。"""
    df = make_ohlcv(n_days=30, n_syms=3)
    out = compute(df, "MA5")
    n_null = out["_factor"].null_count()
    assert n_null == 3 * 4                                # 每组前 4 行


def test_warmup_is_null_not_nan():
    df = make_ohlcv(n_syms=1)
    for name in ("MA20", "BETA5", "RSQR60", "IMAX5", "RANK10"):
        got = compute(df, name)["_factor"]
        warm = got.head(4)                                 # 至少前 4 行无值
        assert warm.null_count() == 4, f"{name} warmup 应为 null"
        assert not warm.is_nan().any(), f"{name} warmup 出现 NaN"


def test_all_windows_compute():
    """29 个 rolling 族 × 全部窗口批量算一遍，无异常、无 inf。"""
    from lquant.factors.qlib_alpha import compute_all

    out = compute_all(make_ohlcv(n_days=80, n_syms=2))
    factor_cols = [c for c in out.columns if c not in
                   {"trade_date", "symbol", "open", "high", "low", "close",
                    "pre_close", "volume", "amount"}]
    assert len(factor_cols) == 158 + 5                     # +5 个 _prep 中间列
    for c in factor_cols[:20]:
        s = out[c].drop_nulls()
        assert s.len() > 0
        assert not (s.abs() > 1e12).any(), c
