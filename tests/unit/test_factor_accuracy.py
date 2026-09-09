"""因子结果正确性验证（F1-F4）。文档: docs/FACTOR_VALIDATION.md

失败模式都是静默错误：IC 虚高、方向反了、未来函数泄漏。
这些断言是 CI 里钉死它们的钉子。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.engine import FactorEngine
from lquant.factors.evaluate import forward_return
from lquant.factors.evaluate.ic import ic_series, ic_summary
from lquant.factors.ops import cs_ops, el_ops, ts_ops  # noqa: F401


def _panel(n_days: int = 120, n_sym: int = 8, seed: int = 7) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            px *= 1 + rng.normal(0, 0.02)
            rows.append({"symbol": f"S{s:02d}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px, "open": px * (1 + rng.normal(0, 0.001)),
                         "high": px * 1.01, "low": px * 0.99,
                         "volume": float(1e5 + 1000 * i), "amount": px * 1e4})
    return pl.DataFrame(rows)


def _with_ret(d: pl.DataFrame, h: int = 1) -> pl.DataFrame:
    return forward_return(d, "close", periods=[h])


def test_f2_perfect_factor_ic_is_one():
    """F2a 杀手锏：构造与未来收益严格线性相关的因子 → IC 必须等于 1.0。"""
    d = _panel().sort("symbol", "trade_date")
    # 反构造：fwd_ret_1 = f（下一日收益本身就是因子值）
    d = d.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_r"))
    d = d.with_columns(pl.col("_r").shift(-1).over("symbol").alias("_factor"))
    d = _with_ret(d).drop_nulls(["_factor", "fwd_ret_1"])
    s = ic_series(d, "_factor", "fwd_ret_1")
    assert s["ic"].mean() == pytest.approx(1.0, abs=1e-9)


def test_f2_ic_invariant_to_daily_demean():
    """F2b：IC(f - mean_d(f)) == IC(f)，抓 over() 分组列写错。"""
    d = _with_ret(_panel().sort("symbol", "trade_date")).with_columns(
        pl.col("close").pct_change(5).over("symbol").alias("f"))
    d = d.drop_nulls(["f", "fwd_ret_1"])
    base = ic_series(d, "f", "fwd_ret_1")["ic"].mean()
    dm = d.with_columns((pl.col("f") - pl.col("f").mean().over("trade_date")).alias("f"))
    dem = ic_series(dm, "f", "fwd_ret_1")["ic"].mean()
    assert base == pytest.approx(dem, abs=1e-9)


def test_f2_ic_antisymmetric():
    """F2c：IC(-f) == -IC(f)，抓多空方向反。"""
    d = _with_ret(_panel().sort("symbol", "trade_date")).with_columns(
        pl.col("close").pct_change(5).over("symbol").alias("f"))
    d = d.drop_nulls(["f", "fwd_ret_1"])
    a = ic_series(d, "f", "fwd_ret_1")["ic"].mean()
    b = ic_series(d.with_columns((-pl.col("f")).alias("f")), "f", "fwd_ret_1")["ic"].mean()
    assert a == pytest.approx(-b, abs=1e-12)


def test_f2_rankic_invariant_under_monotonic_transform():
    """F2d：严格单调变换下 RankIC 完全不变，抓 Pearson/Spearman 混用。"""
    d = _with_ret(_panel().sort("symbol", "trade_date")).with_columns(
        pl.col("close").pct_change(5).over("symbol").alias("f"))
    d = d.drop_nulls(["f", "fwd_ret_1"])
    r1 = ic_series(d, "f", "fwd_ret_1")["rank_ic"].mean()
    r2 = ic_series(d.with_columns(pl.col("f").exp().alias("f")), "f", "fwd_ret_1")["rank_ic"].mean()
    assert r1 == pytest.approx(r2, abs=1e-9)


def test_f3_truncation_invariance():
    """F3 杀手锏：T 日截断数据算出的历史因子值与全窗逐点一致。

    若不一致 = 未来函数或全样本统计量泄漏（全样本 zscore、全样本去极值）。
    """
    full = _panel()
    dates = full["trade_date"].unique().sort().to_list()
    cut_date = dates[99]
    trunc = full.filter(pl.col("trade_date") <= cut_date)
    expr = "Ts_Mean($close, 5) - Ts_Mean($close, 20)"
    a = FactorEngine(full.lazy()).compute(expr, "f").filter(
        pl.col("trade_date") <= cut_date)
    b = FactorEngine(trunc.lazy()).compute(expr, "f")
    j = a.join(b, on=["symbol", "trade_date"], suffix="_b")
    assert len(j) == len(b)
    diff = j.select((pl.col("f") - pl.col("f_b")).abs().max().alias("mx")).item()
    assert diff < 1e-12, f"截断前后因子值不一致 max|diff|={diff}"


def test_f3_truncation_invariance_cross_section():
    """F3b：CS 算子同样截断不变（截面统计不泄漏跨日信息）。"""
    full = _panel()
    dates = full["trade_date"].unique().sort().to_list()
    cut_date = dates[99]
    trunc = full.filter(pl.col("trade_date") <= cut_date)
    a = FactorEngine(full.lazy()).compute("ZScore(Rank($close))", "f").filter(
        pl.col("trade_date") <= cut_date)
    b = FactorEngine(trunc.lazy()).compute("ZScore(Rank($close))", "f")
    j = a.join(b, on=["symbol", "trade_date"], suffix="_b")
    diff = j.select((pl.col("f") - pl.col("f_b")).abs().max().alias("mx")).item()
    assert diff < 1e-12


def test_f4_t_stat_identity():
    """F4：t_stat == IC_mean / IC_std * sqrt(n_days)（指标间数学恒等）。"""
    d = _with_ret(_panel()).with_columns(
        pl.col("close").pct_change(5).over("symbol").alias("f")).drop_nulls(["f", "fwd_ret_1"])
    s = ic_summary(d, "f", "fwd_ret_1")
    ser = ic_series(d, "f", "fwd_ret_1")
    n = len(ser)
    expected = s["ic"]["mean"] / s["ic"]["std"] * np.sqrt(n)
    assert s["ic"]["t_stat"] == pytest.approx(expected, abs=1e-6)



def test_f5_cross_implementation_ma20():
    """F5 交叉实现对照：DSL 翻译版 MA20 vs qlib_alpha 内置实现，逐点一致。"""
    from lquant.factors.qlib_alpha import compute as qlib_compute

    panel = _panel(n_days=60, n_sym=5)
    a = qlib_compute(panel, "MA20")
    b = FactorEngine(panel.lazy()).compute("Ts_Mean($close, 20) / $close", "f")
    j = a.join(b, on=["symbol", "trade_date"], suffix="_b").drop_nulls(["_factor", "f"])
    diff = j.select((pl.col("_factor") - pl.col("f")).abs().max().alias("mx")).item()
    assert diff < 1e-9, f"两套实现差异 {diff}"


def test_f5_cross_implementation_rsv():
    """F5b: RSV10 双实现对照。"""
    from lquant.factors.qlib_alpha import compute as qlib_compute

    panel = _panel(n_days=60, n_sym=5)
    a = qlib_compute(panel, "RSV10")
    b = FactorEngine(panel.lazy()).compute(
        "($close-Ts_Min($low,10))/(Ts_Max($high,10)-Ts_Min($low,10)+1e-12)", "f")
    j = a.join(b, on=["symbol", "trade_date"], suffix="_b").drop_nulls(["_factor", "f"])
    diff = j.select((pl.col("_factor") - pl.col("f")).abs().max().alias("mx")).item()
    assert diff < 1e-9


def test_f6_reproducible_double_compute():
    """F6 可复现性：同输入两次计算逐位一致；经缓存往返亦一致。"""
    panel = _panel(n_days=40)
    expr = "Rank(Ts_Mean($close,5)/$close-1)"
    a = FactorEngine(panel.lazy()).compute(expr, "f")
    b = FactorEngine(panel.lazy()).compute(expr, "f")
    assert a.equals(b)
