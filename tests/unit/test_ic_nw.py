"""Newey-West 自相关校正 t 值 + IC method 显式化。"""
from __future__ import annotations

import math
import random

import polars as pl

from lquant.factors.evaluate.ic import ic_summary, newey_west_tstat


def _golden_df() -> pl.DataFrame:
    """与 test_factor_golden 的 DF 同构：5 只股票 × 10 天，mom 现算。

    本地 helper，不跨测试文件 import。
    """
    prices = {
        "A": [10.0 * 1.1 ** i for i in range(10)],
        "B": [10.0] * 10,
        "C": [10.0 * 0.9 ** i for i in range(10)],
        "D": [10.0 + (1.0 if i % 2 else -1.0) for i in range(10)],
        "E": [10.0 + 0.1 * i for i in range(10)],
    }
    df = pl.DataFrame([
        {"symbol": s, "trade_date": d, "close": prices[s][d - 1]}
        for d in range(1, 11) for s in ["A", "B", "C", "D", "E"]
    ])
    return df.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("mom")
    )


def _t_ols(x) -> float:
    """朴素 t = mean / (std/√n)，手工算，不依赖被测代码。"""
    n = len(x)
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / (n - 1)
    return m / math.sqrt(var / n)


def test_nw_white_noise_close_to_ols():
    random.seed(7)
    x = [random.gauss(0, 1) for _ in range(500)]
    assert abs(newey_west_tstat(x) - _t_ols(x)) < 0.3


def test_nw_shrinks_positive_autocorr_series():
    # 强正自相关（AR(1) φ=0.7）→ OLS t 高估严重，NW 应显著更小
    random.seed(3)
    e = [random.gauss(0, 1) for _ in range(600)]
    x, v = [], 0.0
    for eps in e:
        v = 0.7 * v + eps
        x.append(0.05 + v)
    ols_t = _t_ols(x)
    nw = newey_west_tstat(x)
    assert abs(nw) < abs(ols_t) * 0.8
    assert math.isfinite(nw)


def test_ic_summary_reports_method_and_nw():
    df = _golden_df().with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("fwd_ret_1")
    )
    s = ic_summary(df, "mom", "fwd_ret_1")
    assert s["method"] == "both"
    assert "t_stat_nw" in s["ic"]
    # 向后兼容：旧键仍在
    assert "t_stat" in s["ic"] and "rank_ic" in s
    # NW t 应为有限值（IC 序列足够长）
    assert math.isfinite(s["ic"]["t_stat_nw"])


def test_ic_method_pearson_only():
    df = _golden_df().with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("fwd_ret_1")
    )
    s = ic_summary(df, "mom", "fwd_ret_1", method="pearson")
    assert "rank_ic" not in s
    assert s["method"] == "pearson"


def test_ic_method_spearman_only():
    df = _golden_df().with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("fwd_ret_1")
    )
    s = ic_summary(df, "mom", "fwd_ret_1", method="spearman")
    assert "rank_ic" in s
    assert s["method"] == "spearman"


def test_nw_edge_cases():
    assert math.isnan(newey_west_tstat([]))
    assert math.isnan(newey_west_tstat([1.0]))
    # 完全恒定序列 → lrv = 0 → nan
    assert math.isnan(newey_west_tstat([1.0] * 50))
    # 显式 lags
    random.seed(1)
    x = [random.gauss(0, 1) for _ in range(100)]
    assert math.isfinite(newey_west_tstat(x, lags=3))
