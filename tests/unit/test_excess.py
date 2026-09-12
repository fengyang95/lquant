"""超额收益体系：几何超额口径的年化超额 / 超额夏普 / 超额回撤。"""
from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from lquant.factors.evaluate.excess import (
    benchmark_series,
    excess_perf,
    excess_returns,
    group_excess_summary,
    quantile_excess_nav,
)


def test_excess_returns_geometric():
    r = np.array([0.10, 0.00])
    b = np.array([0.00, -0.50])
    ex = excess_returns(r, b)
    # (1.10/1.00 - 1, 1.00/0.50 - 1) —— 基准腰斩时跑平 = 大幅超额
    assert ex[0] == pytest.approx(0.10)
    assert ex[1] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        excess_returns(r, b[:-1])


def test_benchmark_series_equal_weight():
    df = pl.DataFrame({
        "trade_date": [1, 1, 2, 2],
        "fwd_ret_1": [0.02, 0.04, None, 0.06],
    })
    bs = benchmark_series(df, "fwd_ret_1")
    assert bs["bench"].to_list() == pytest.approx([0.03, 0.06])   # None 不进均值


def test_excess_perf_known_values():
    # 每天组合 +1%、基准 +0.5% → 日几何超额 = 1.01/1.005 - 1（≠ 0.5%，别混口径）
    n = 252
    r = np.full(n, 0.01)
    b = np.full(n, 0.005)
    p = excess_perf(r, b)
    assert p["annual_return"] == pytest.approx((1.01 / 1.005) ** 252 - 1, rel=1e-6)
    # 零波动超额序列：夏普按 NaN 口径（perf_from_returns 对 vol=0 不给伪无穷大）
    assert p["max_drawdown"] == 0.0


def test_group_excess_summary_shapes():
    rng = np.random.default_rng(7)
    rows = []
    for d in range(1, 31):
        for i in range(6):
            rows.append({"symbol": f"S{i}", "trade_date": d,
                         "f": float(i), "fwd_ret_1": float(rng.normal(0, 0.02))})
    df = pl.DataFrame(rows)
    out = group_excess_summary(df, "f", "fwd_ret_1", n_groups=3)
    assert len(out) == 3
    assert set(out.columns) >= {"q", "annual_excess", "excess_sharpe", "excess_mdd"}
    assert all(math.isfinite(v) for v in out["annual_excess"].to_list())


def test_quantile_excess_nav_long_short_ratio():
    # 组 2 每日 +2%、组 1 每日 0%、基准恒 1%：
    # ex_q2 单调升（跑赢），ex_q1 单调降（跑输），ex_long_short 升
    rows = []
    for d in range(1, 21):
        rows.append({"symbol": "A", "trade_date": d, "f": 2.0, "fwd_ret_1": 0.02})
        rows.append({"symbol": "B", "trade_date": d, "f": 1.0, "fwd_ret_1": 0.0})
    df = pl.DataFrame(rows)
    nav = quantile_excess_nav(df, "f", "fwd_ret_1", n_groups=2)
    ex2 = nav["ex_q2"].to_list()
    ex1 = nav["ex_q1"].to_list()
    ls = nav["ex_long_short"].to_list()
    assert ex2[-1] > ex2[0] > 1.0
    assert ex1[-1] < 1.0
    assert ls[-1] > ls[0]
    # 曲线终点 = 组净值 / 基准净值（几何超额的定义）
    assert ex2[-1] == pytest.approx(1.02 ** 20 / 1.01 ** 20, rel=1e-6)
