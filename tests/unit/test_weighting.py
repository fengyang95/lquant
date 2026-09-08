"""组合权重模块测试：等权 / 逆波动 / 风险平价 / 最小方差 / HRP。"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.portfolio.weighting import (
    equal_weight,
    hrp_weight,
    inverse_vol_weight,
    min_variance_weight,
    risk_parity_weight,
    weights,
)


def make_returns(n_days: int = 500, seed: int = 3) -> pl.DataFrame:
    """两只低波动 + 两只高波动，互相有一定相关性。"""
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.01, n_days)
    b = rng.normal(0, 0.01, n_days)
    c = rng.normal(0, 0.03, n_days)
    d = 0.5 * c + rng.normal(0, 0.02, n_days)
    return pl.DataFrame({"A": a, "B": b, "C": c, "D": d})


def _vec(w: dict[str, float]) -> np.ndarray:
    return np.array([w[k] for k in sorted(w)])


# ---------- 基础 ----------

def test_equal_weight():
    r = make_returns()
    w = equal_weight(r)
    assert len(w) == 4
    assert sum(w.values()) == pytest.approx(1.0)


def test_inverse_vol_prefers_low_vol():
    r = make_returns()
    w = inverse_vol_weight(r)
    assert w["A"] > w["C"]          # 低波动权重更高
    assert sum(w.values()) == pytest.approx(1.0)


# ---------- 优化类 ----------

def test_risk_parity_equal_risk_contribution():
    r = make_returns()
    w = risk_parity_weight(r)
    wv = _vec(w)
    assert wv.sum() == pytest.approx(1.0, abs=1e-6)
    cov = np.cov(np.column_stack([r[c].to_numpy() for c in sorted(w)]), rowvar=False)
    sigma = float(np.sqrt(wv @ cov @ wv))
    rc = wv * (cov @ wv) / sigma
    # 等风险贡献：rc 两两差异很小
    assert float(np.std(rc)) < 0.15 * float(np.mean(rc))


def test_min_variance_lower_than_equal():
    r = make_returns()
    w_min = _vec(min_variance_weight(r))
    w_eq = _vec(equal_weight(r))
    cov = np.cov(np.column_stack([r[c].to_numpy() for c in sorted(min_variance_weight(r))]),
                 rowvar=False)
    assert w_min.sum() == pytest.approx(1.0, abs=1e-6)
    assert w_min @ cov @ w_min <= w_eq @ cov @ w_eq + 1e-10   # 优化不应更差


def test_hrp_sums_to_one_and_diversifies():
    r = make_returns()
    w = hrp_weight(r)
    wv = _vec(w)
    assert wv.sum() == pytest.approx(1.0, abs=1e-6)
    assert (wv > 0).all()
    # C/D 相关性高（0.5*），组合不会全押在单一高波票上
    assert w["C"] < 0.5 and w["D"] < 0.5


# ---------- 统一入口 ----------

def test_weights_dispatch():
    r = make_returns()
    for method in ("equal", "inverse_vol", "risk_parity", "min_variance", "hrp"):
        w = weights(r, method)
        assert w and sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_weights_unknown_method_falls_back_to_equal():
    """设计约定：未知方法退回等权而不是报错（权重算不出来不该崩流程）。"""
    r = make_returns()
    w = weights(r, "no_such_method")
    assert len(w) == 4
    assert sum(w.values()) == pytest.approx(1.0)


def test_nan_rows_are_ignored():
    r = make_returns(50)
    r = r.with_columns(pl.when(pl.arange(0, 50) % 7 == 0).then(None)
                       .otherwise(pl.col("A")).alias("A"))
    w = inverse_vol_weight(r)
    assert sum(w.values()) == pytest.approx(1.0)
