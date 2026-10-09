"""尾部风险：CVaR / CDaR 的统计量与其 LP 优化。

两条原则先钉住：
1. **统计量口径**：CVaR 是尾部条件期望（负值=亏损），CDaR 是回撤的条件期望
   （正值=回撤幅度）—— 数值断言直接对着定义写；
2. **优化真的降风险**：LP 解出来的组合，其 CVaR/CDaR 必须**不差于等权**，
   且高波动标的权重被压低。求解器返回一个"能跑通"的解不算数。
"""
from __future__ import annotations

import numpy as np
import pytest

from lquant.portfolio.tailrisk import (
    cdar,
    cdar_weight_lp,
    cvar,
    cvar_weight_lp,
    tail_metrics,
)


def _returns(seed: int = 0, t: int = 300, n: int = 3, *, vol_scale: float = 3.0):
    rng = np.random.default_rng(seed)
    R = rng.normal(0.0005, 0.02, size=(t, n))
    if n > 1:
        R[:, -1] *= vol_scale
    return R


# ---------- 统计量 ----------

def test_cvar_is_mean_of_the_tail():
    r = np.array([-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.1,
                  0.02, 0.01, 0.0, -0.01, 0.03, 0.02, 0.01, 0.0, 0.02, 0.01])
    # level=0.9 → 最差 10% 即 2 个观测：-0.05 与 -0.04
    assert cvar(r, 0.9) == pytest.approx(-0.045)


def test_cvar_and_cdar_are_negative_and_positive_respectively():
    r = _returns()[:, 0]
    assert cvar(r) < 0                      # 亏损记负
    assert cdar(r) > 0                      # 回撤记正
    # 更极端的尾部一定更差
    assert cvar(r, 0.99) <= cvar(r, 0.95)
    assert cdar(r, 0.99) >= cdar(r, 0.95)


def test_cdar_is_zero_for_monotone_up_series():
    assert cdar(np.full(50, 0.01)) == pytest.approx(0.0, abs=1e-12)


def test_tail_metrics_reports_both_levels():
    out = tail_metrics(_returns()[:, 0])
    assert set(out) == {"cvar_095", "cdar_095", "cvar_099", "cdar_099"}
    assert out["cvar_099"] <= out["cvar_095"]


@pytest.mark.parametrize("bad", [0.5, 1.0, 0.4, 1.5])
def test_level_must_be_in_open_interval(bad):
    with pytest.raises(ValueError, match="level"):
        cvar(_returns()[:, 0], bad)


def test_too_few_observations_raises():
    with pytest.raises(ValueError, match="观测"):
        cvar(np.array([0.01, -0.02, 0.03]))
    with pytest.raises(ValueError, match="观测"):
        cdar(np.array([0.01, -0.02, 0.03]))


def test_non_finite_returns_are_dropped():
    r = np.concatenate([_returns()[:, 0], [np.nan, np.inf, -np.inf]])
    assert np.isfinite(cvar(r))


# ---------- LP 优化 ----------

def test_cvar_lp_beats_equal_weight_and_shrinks_the_risky_asset():
    R = _returns()
    w = cvar_weight_lp(R)
    assert w.sum() == pytest.approx(1.0)
    assert (w >= -1e-12).all()
    assert cvar(R @ w) > cvar(R.mean(axis=1))          # 尾部亏损更小（更接近 0）
    assert w[-1] < w[:-1].mean()                       # 高波动标的被压低


def test_cdar_lp_reduces_drawdown_tail():
    R = _returns()
    w = cdar_weight_lp(R, lookback=200)
    assert w.sum() == pytest.approx(1.0)
    assert cdar(R @ w) < cdar(R.mean(axis=1))
    assert w[-1] < w[:-1].mean()


def test_max_weight_cap_is_respected():
    R = _returns(n=4)
    w = cvar_weight_lp(R, max_weight=0.4)
    assert w.max() <= 0.4 + 1e-9


def test_min_return_constraint_tilts_the_solution():
    """加收益约束后，权重必须往收益更高的标的上挪（约束真的生效）。"""
    R = _returns(n=3)
    base = cvar_weight_lp(R)
    # 阈值要从数据推出来：写死一个比可达收益还高的数会直接不可行
    feasible = float(R.mean(axis=0).max()) * 0.6
    tilted = cvar_weight_lp(R, min_return=feasible)
    best = int(np.argmax(R.mean(axis=0)))
    assert tilted[best] >= base[best] - 1e-9
    assert float(R.mean(axis=0) @ tilted) >= feasible - 1e-9   # 约束真的成立
    # 要得比能给的还高 → 明确报「不可行」，不是含糊的「未收敛」
    with pytest.raises(RuntimeError, match="不可行"):
        cvar_weight_lp(R, min_return=float(R.max()) * 10)


@pytest.mark.parametrize("solver", [cvar_weight_lp, cdar_weight_lp])
def test_lp_rejects_bad_input(solver):
    with pytest.raises(ValueError, match="T×N"):
        solver(np.array([0.01, -0.02]))
    with pytest.raises(ValueError, match="至少需要 2 个标的"):
        solver(_returns(n=1))
    with pytest.raises(ValueError, match="level"):
        solver(_returns(), level=0.3)


# ---------- 接进 weighting 注册表 ----------

def test_weighting_registers_tail_methods_and_raises_loudly():
    from lquant.portfolio.optimizer import OptimizerError
    from lquant.portfolio.weighting import METHODS, weights

    assert {"cvar", "cdar"} <= set(METHODS)
    syms = ["A", "B", "C", "D"]
    R = _returns(n=4)
    w = weights(R, "cvar", syms)
    assert set(w) == set(syms) and sum(w.values()) == pytest.approx(1.0)
    # 与 min_variance 的静默回退不同：尾风险算不出来就抛错，不换口径
    with pytest.raises(OptimizerError, match="cvar 输入不合法"):
        weights(_returns(t=10, n=3), "cvar", ["A", "B", "C"])
