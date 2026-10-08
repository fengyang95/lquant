"""仓位模型：ATR 风险预算与 Kelly 公式的数学性质。"""

from __future__ import annotations

import math

import pytest

from lquant.portfolio.sizing import atr_weight, kelly_fraction

# ---------- ATR 风险预算 ----------


def test_atr_weight_exact_formula():
    # daily_risk * close / ATR = 0.01 * 10 / 0.5 = 0.2
    assert atr_weight(10.0, 0.5) == pytest.approx(0.2)


def test_atr_weight_monotone_in_volatility():
    """波动越大仓位越小 —— 风险预算的核心不变量。"""
    w_low = atr_weight(10.0, 0.3)
    w_high = atr_weight(10.0, 1.5)
    assert w_low > w_high


def test_atr_weight_caps_at_max_weight():
    assert atr_weight(10.0, 0.001) == 1.0
    assert atr_weight(10.0, 0.1, max_weight=0.3) == 0.3


def test_atr_weight_never_negative():
    assert atr_weight(10.0, 5.0) > 0


@pytest.mark.parametrize(
    "close,atr,risk",
    [
        (0.0, 0.5, 0.01),
        (-1.0, 0.5, 0.01),
        (10.0, 0.0, 0.01),
        (10.0, -0.5, 0.01),
        (10.0, 0.5, 0.0),
    ],
)
def test_atr_weight_rejects_bad_inputs(close, atr, risk):
    """脏数据显式报错，不静默夹逼成「保守决策」。"""
    with pytest.raises(ValueError):
        atr_weight(close, atr, daily_risk=risk)


@pytest.mark.parametrize(
    "close,atr,risk",
    [
        (float("nan"), 0.5, 0.01),
        (10.0, float("nan"), 0.01),
        (10.0, 0.5, float("nan")),
        (float("inf"), 0.5, 0.01),
        (10.0, float("inf"), 0.01),
    ],
)
def test_atr_weight_rejects_nan_inf(close, atr, risk):
    """NaN 的比较恒为 False，`<= 0` 守卫拦不住它 —— 必须 isfinite 显式拦截。

    修复前 ``atr_weight(nan, 0.5)`` 原样返回 NaN（``min(nan, 1.0) = nan``），
    脏数据穿透到权重层。
    """
    with pytest.raises(ValueError):
        atr_weight(close, atr, daily_risk=risk)


@pytest.mark.parametrize("bad", [-1.0, 0.0, -0.5, 1.5, float("nan")])
def test_atr_weight_rejects_out_of_range_max_weight(bad):
    """max_weight 越界会让 ``min(raw, max_weight)`` 直接吐出负权重/超配。

    契约是「返回 ∈ [0, max_weight] 且绝不为负」，max_weight ≤ 0 必须在入口
    拦下，而不是把 -1.0 当仓位返回。
    """
    with pytest.raises(ValueError):
        atr_weight(10.0, 0.5, max_weight=bad)


# ---------- Kelly ----------


def test_kelly_textbook_values():
    assert kelly_fraction(0.6, 1.0, fraction=1.0) == pytest.approx(0.2)
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.1)  # half-Kelly 默认
    # p=0.55, b=2 → 0.55 - 0.45/2 = 0.325
    assert kelly_fraction(0.55, 2.0, fraction=1.0) == pytest.approx(0.325)


def test_kelly_matches_numerical_argmax_of_log_growth():
    """一阶条件数值验证：f* 应最大化 E[log 增长率]。

    E(f) = p·ln(1+b·f) + (1-p)·ln(1-f)，网格搜索 argmax 与解析解对齐。
    """
    p, b = 0.6, 1.5
    grid = [i / 1000 for i in range(1, 1000)]
    growth = [p * math.log(1 + b * f) + (1 - p) * math.log(1 - f) for f in grid]
    best = grid[max(range(len(grid)), key=growth.__getitem__)]
    assert kelly_fraction(p, b, fraction=1.0) == pytest.approx(best, abs=0.002)


def test_kelly_no_edge_returns_zero_not_negative():
    """期望劣势 → 0（不下注是决策），绝不返回负权重。"""
    assert kelly_fraction(0.5, 1.0, fraction=1.0) == 0.0
    assert kelly_fraction(0.3, 2.0, fraction=1.0) == 0.0  # 0.3-0.35 < 0


@pytest.mark.parametrize(
    "p,b,fraction",
    [
        (0.0, 1.0, 0.5),
        (1.0, 1.0, 0.5),
        (-0.1, 1.0, 0.5),
        (0.6, 0.0, 0.5),
        (0.6, -1.0, 0.5),
        (0.6, 1.0, 0.0),
        (0.6, 1.0, 1.5),
    ],
)
def test_kelly_rejects_bad_inputs(p, b, fraction):
    with pytest.raises(ValueError):
        kelly_fraction(p, b, fraction=fraction)


def test_sizing_models_are_reachable_from_package():
    """``atr_weight``/``kelly_fraction`` 必须从 ``lquant.portfolio`` 可达。

    修复前它们只在 ``portfolio.sizing`` 里，``__all__`` 也不含 —— 能力存在
    但外面拿不到（零调用方正是这么来的）。
    """
    import lquant.portfolio as pkg

    assert pkg.atr_weight is atr_weight
    assert pkg.kelly_fraction is kelly_fraction
    assert "atr_weight" in pkg.__all__
    assert "kelly_fraction" in pkg.__all__
