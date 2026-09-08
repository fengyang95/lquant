"""归因分析测试：贡献守恒、Brinson 加总=超额、风险指标。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest.attribution import (brinson_by_group, group_of_symbol,
                                         risk_vs_benchmark, stock_contribution)


def _scenario():
    """两天、两股：day1 买入 A 全仓，day2 A +5% / B -3%。

    nav: day1 = 1_000_000（当日买入，收盘价=开盘价，无浮盈）
         day2 = 1_000_000 + 持仓 9900 股 × 105 - 费用（零费假设 990,000→1,039,500）
    """
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    positions = {d1: {"600000.SH": 9900.0}, d2: {"600000.SH": 9900.0}}
    prices = {"600000.SH": {d1: 100.0, d2: 105.0},
              "000001.SZ": {d1: 50.0, d2: 48.5}}
    nav = [(d1, 1_000_000.0), (d2, 500_000.0 + 9900 * 105.0)]
    return d1, d2, positions, prices, nav


def test_stock_contribution_sums_to_total():
    """个股贡献 + 现金/择时残差 ≈ 当日总收益（守恒）。"""
    d1, d2, positions, prices, nav = _scenario()
    stocks, residual = stock_contribution(positions, prices, nav)
    assert len(stocks) == 1
    total_ret = nav[1][1] / nav[0][1] - 1
    explained = stocks[0]["contribution"] + residual[0]["residual"]
    assert explained == pytest.approx(total_ret, abs=1e-6)
    # 个股贡献本身 = 权重 × 涨幅 = (990000/1000000) × 5%
    assert stocks[0]["contribution"] == pytest.approx(0.99 * 0.05, abs=1e-6)


def test_brinson_group_sum_equals_excess():
    """分组 Brinson：Σ(配置+选股+交互) 应与组合相对基准的总超额一致（同口径）。"""
    days = [date(2026, 1, 5 + i) for i in range(4)]
    prices = {
        "600000.SH": {days[i]: 100 * (1.01 ** i) for i in range(4)},
        "600519.SH": {days[i]: 100 * (1.03 ** i) for i in range(4)},
        "000001.SZ": {days[i]: 50 * (0.99 ** i) for i in range(4)},
        "300750.SZ": {days[i]: 80 * (1.02 ** i) for i in range(4)},
    }
    # 组合：满仓沪市两股；基准池：全部 4 只等权
    positions = {d: {"600000.SH": 5000.0, "600519.SH": 5000.0} for d in days}
    nav = [(days[0], 1_000_000.0)]
    for i in range(1, 4):
        v = 5000 * prices["600000.SH"][days[i]] + 5000 * prices["600519.SH"][days[i]]
        nav.append((days[i], v))
    group_map = {s: group_of_symbol(s) for s in prices}
    out = brinson_by_group(positions, prices, nav, list(prices), group_map)
    assert out["groups"]
    total = sum(g["total"] for g in out["groups"])
    # 手动算组合 vs 基准超额（每日 Σ w_i r_i - Σ wb_i rb_i，日复利差）
    port_ret, bench_ret = 1.0, 1.0
    for i in range(1, 4):
        d = days[i]
        pr = 0.5 * (prices["600000.SH"][d] / prices["600000.SH"][days[i - 1]] - 1) \
           + 0.5 * (prices["600519.SH"][d] / prices["600519.SH"][days[i - 1]] - 1)
        br = sum(prices[s][d] / prices[s][days[i - 1]] - 1 for s in prices) / 4
        port_ret *= 1 + pr
        bench_ret *= 1 + br
    manual_excess = port_ret / bench_ret - 1
    # 算术 Brinson 与几何超额同数量级、符号一致（口径差在 2% 以内）
    assert total == pytest.approx(manual_excess, abs=0.02)


def test_group_of_symbol_boards():
    assert group_of_symbol("600000.SH") == "沪市主板"
    assert group_of_symbol("000001.SZ") == "深市主板"
    assert group_of_symbol("300750.SZ") == "创业板"
    assert group_of_symbol("688981.SH") == "科创板"
    assert group_of_symbol("832000.BJ") == "北交所"


def test_risk_vs_benchmark_alpha_beta():
    """β=1 的完美跟踪：α≈0、β≈1；加恒定日超额 → IR 高。"""
    import random

    random.seed(42)
    bench = [random.gauss(0, 0.01) for _ in range(120)]
    strat = [b + 0.0005 for b in bench]          # β=1，每日恒定超额 5bp
    out = risk_vs_benchmark(strat, bench)
    assert out["beta"] == pytest.approx(1.0, abs=0.01)
    assert out["alpha_annual"] == pytest.approx(0.0005 * 252, rel=0.05)
    # 恒定日超额 → 超额波动 0 → IR 记 None（∞ 语义）；总超额必为正
    assert out["information_ratio"] is None or out["information_ratio"] > 3
    assert out["excess_return"] > 0
