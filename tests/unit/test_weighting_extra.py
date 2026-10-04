"""portfolio/weighting.py 补充单测：分数/市值加权、统一入口、对比报告、降级路径。"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.portfolio.weighting import (
    METHODS,
    _clean,
    _returns_matrix,
    hrp_weight,
    inverse_vol_weight,
    market_cap_weight,
    min_variance_weight,
    risk_parity_weight,
    score_weight,
    weight_report,
    weights,
)


def make_returns_df(n_days: int = 300, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.01, n_days)
    b = rng.normal(0, 0.02, n_days)
    c = 0.5 * a + rng.normal(0, 0.01, n_days)
    d = rng.normal(0, 0.03, n_days)
    return pl.DataFrame({"A": a, "B": b, "C": c, "D": d})


# ---------- _returns_matrix / _clean ----------

def test_returns_matrix_df_and_ndarray_paths():
    df = make_returns_df()
    M, syms = _returns_matrix(df)
    assert syms == ["A", "B", "C", "D"] and M.shape == (300, 4)

    M2, syms2 = _returns_matrix(df, symbols=["B", "A"])
    assert syms2 == ["B", "A"] and M2.shape == (300, 2)

    arr, syms3 = _returns_matrix(np.eye(5) * 0.01)
    assert syms3 == [f"a{i}" for i in range(5)]
    assert arr.shape == (5, 5)


def test_returns_matrix_nan_filled():
    df = make_returns_df(5).with_columns(pl.lit(None).alias("A"))
    M, _ = _returns_matrix(df)
    assert np.isfinite(M).all()


def test_clean_edge_cases():
    np.testing.assert_allclose(_clean(np.array([np.nan, np.inf, -np.inf])),
                               [1 / 3, 1 / 3, 1 / 3])      # 全失效 → 等权
    np.testing.assert_allclose(_clean(np.array([2.0, 2.0])), [0.5, 0.5])
    w = _clean(np.array([-1.0, 2.0]))
    assert w[0] == 0.0 and w.sum() == pytest.approx(1.0)


# ---------- score_weight / market_cap_weight ----------

def test_score_weight_positive_normalization():
    df = pl.DataFrame({"symbol": ["A", "B", "C"], "score": [1.0, 3.0, 4.0]})
    w = score_weight(df)
    assert w == {"A": 0.125, "B": 0.375, "C": 0.5}
    # 负分被截为 0
    w2 = score_weight(pl.DataFrame({"symbol": ["A", "B"], "score": [-5.0, 1.0]}))
    assert w2["A"] == 0.0 and w2["B"] == 1.0


def test_score_weight_all_nonpositive_falls_back_to_equal():
    df = pl.DataFrame({"symbol": ["A", "B"], "score": [-1.0, -2.0]})
    w = score_weight(df)
    assert w == {"A": 0.5, "B": 0.5}
    assert w == {"A": 0.5, "B": 0.5}


def test_score_weight_empty():
    df = pl.DataFrame({"symbol": [None], "score": [None]})
    assert score_weight(df) == {}


def test_score_weight_positive_only_false_keeps_negative():
    df = pl.DataFrame({"symbol": ["A", "B"], "score": [-1.0, 3.0]})
    w = score_weight(df, positive_only=False)
    assert w["A"] < 0 and w["B"] > 0 and sum(w.values()) == pytest.approx(1.0)


def test_market_cap_weight_and_sqrt():
    df = pl.DataFrame({"symbol": ["A", "B"], "market_cap": [1e8, 3e8]})
    w = market_cap_weight(df)
    assert w["B"] / w["A"] == pytest.approx(3.0)
    w2 = market_cap_weight(df, sqrt=True)
    assert w2["B"] / w2["A"] == pytest.approx(np.sqrt(3.0))


def test_market_cap_weight_negative_cap_clipped():
    df = pl.DataFrame({"symbol": ["A", "B"], "market_cap": [-5.0, 5.0]})
    w = market_cap_weight(df)
    assert w["A"] == 0.0 and w["B"] == 1.0


def test_market_cap_weight_empty():
    df = pl.DataFrame({"symbol": [None], "market_cap": [None]})
    assert market_cap_weight(df) == {}


# ---------- 优化方法与降级 ----------

def test_weights_unknown_method_falls_back_to_equal():
    w = weights(make_returns_df(), method="nonexistent")
    assert all(v == pytest.approx(0.25) for v in w.values())
    # 已知方法直连
    w2 = weights(make_returns_df(), method="equal")
    assert all(v == pytest.approx(0.25) for v in w2.values())


def test_methods_registry_complete():
    assert set(METHODS) == {"equal", "inverse_vol", "risk_parity",
                            "min_variance", "hrp", "enhanced_indexing"}


#: 需要额外输入的方法：走通用入口时必须显式给这些 kw，见下面两个测试。
NEEDS_INPUT = {
    # enhanced_indexing 只给风险约束、不给 α 视图时目标函数无意义，
    # 所以要求 scores / expected_returns（基准可省，缺省等权基准）
    "enhanced_indexing": {"scores": {"A": 1.0, "B": 0.0, "C": -1.0, "D": -2.0}},
}


def test_weights_passes_kwargs():
    r = make_returns_df()
    for name in METHODS:
        w = weights(r, method=name, **NEEDS_INPUT.get(name, {}))
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_weights_missing_view_raises_not_silently_equal():
    """缺输入要报错，不能静默退回等权 —— 那会让人以为约束生效了。"""
    from lquant.portfolio.optimizer import OptimizerError

    with pytest.raises(OptimizerError, match="scores"):
        weights(make_returns_df(), method="enhanced_indexing")


def test_risk_parity_degrades_on_singular_cov():
    """协方差奇异（全同列）→ SLSQP 失败 → 降级逆波动率。"""
    n = 40
    col = np.full((n, 1), 0.01)
    M = np.repeat(col, 3, axis=1)
    w = risk_parity_weight(M)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_min_variance_max_weight_constraint():
    r = make_returns_df()
    w = min_variance_weight(r, max_weight=0.3)
    assert max(w.values()) <= 0.3 + 1e-6


def test_min_variance_degrades_on_singular_cov():
    n = 40
    M = np.repeat(np.full((n, 1), 0.01), 3, axis=1)
    w = min_variance_weight(M)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_hrp_small_input_falls_back_to_inverse_vol():
    # n<3 → 直接逆波动
    M = np.column_stack([np.full(10, 0.01), np.full(10, 0.02)])
    w = hrp_weight(M)
    iv = inverse_vol_weight(M)
    assert w == iv


def test_hrp_linkage_failure_falls_back_to_order():
    """linkage 输入退化（全零距离）→ order 退化为自然顺序，权重仍归一。"""
    M = np.zeros((10, 4))
    w = hrp_weight(M)
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_weight_report_columns_and_sort():
    r = make_returns_df()
    rep = weight_report(r, methods=["equal", "inverse_vol"])
    # 输出统一按 vol 升序
    assert sorted(rep["method"].to_list()) == ["equal", "inverse_vol"]
    assert rep["vol"].to_list() == sorted(rep["vol"].to_list())
    rep2 = weight_report(r)  # 默认全部方法，按 vol 升序
    assert set(rep2["method"].to_list()) == set(METHODS)
    assert set(rep2.columns) >= {"method", "vol", "effective_n",
                                 "max_weight", "n_holdings", "note"}
    # 需要 α 视图的方法算不出来 → 指标留空 + note 说明，而不是让整张报告炸
    ok_rows = rep2.filter(pl.col("vol").is_not_null())
    assert ok_rows["vol"].to_list() == sorted(ok_rows["vol"].to_list())
    assert (ok_rows["n_holdings"] == 4).all()
    skipped = rep2.filter(pl.col("vol").is_null())
    assert skipped["method"].to_list() == ["enhanced_indexing"]
    assert "scores" in skipped["note"][0]


def test_weight_report_tiny_sample_uses_identity_cov():
    r = make_returns_df(2)
    rep = weight_report(r)
    assert rep.height == len(METHODS)
    # 样本太小 → 走单位协方差兜底；缺 α 视图的那一行没有 vol，排除掉再看
    assert (rep.filter(pl.col("vol").is_not_null())["vol"] > 0).all()


# ---------- 强制注入异常覆盖降级分支 ----------

def test_risk_parity_nonconverged_degrades(monkeypatch):
    """SLSQP 报未收敛 → 降级逆波动率。"""

    def fail_minimize(*a, **kw):
        class Res:
            success = False
            x = None
        return Res()

    monkeypatch.setattr("scipy.optimize.minimize", fail_minimize)
    r = make_returns_df()
    w = risk_parity_weight(r)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_min_variance_nonconverged_degrades(monkeypatch):

    def fail_minimize(*a, **kw):
        class Res:
            success = False
            x = None
        return Res()

    monkeypatch.setattr("scipy.optimize.minimize", fail_minimize)
    r = make_returns_df()
    w = min_variance_weight(r)
    iv = inverse_vol_weight(r)
    assert w == iv


def test_hrp_linkage_error_uses_natural_order(monkeypatch):
    import scipy.cluster.hierarchy as hier

    def bad_linkage(*a, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(hier, "linkage", bad_linkage)
    r = make_returns_df()
    w = hrp_weight(r)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)
    assert all(v > 0 for v in w.values())


def test_hrp_zero_variance_alpha_half_branch():
    """全零收益 → 每簇方差 0 → alpha=0.5 分支。"""
    M = np.zeros((6, 4))
    w = hrp_weight(M)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    np.testing.assert_allclose(sorted(w.values()), [0.25] * 4)


def test_hrp_without_scipy_falls_back(monkeypatch):
    """无 scipy 层次聚类时（import 失败）退回逆波动率。"""
    import sys

    monkeypatch.setitem(sys.modules, "scipy.cluster.hierarchy", None)
    monkeypatch.setitem(sys.modules, "scipy.spatial.distance", None)
    w = hrp_weight(make_returns_df())
    assert w == inverse_vol_weight(make_returns_df())
