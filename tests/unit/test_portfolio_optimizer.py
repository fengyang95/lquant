"""基准相对优化（Phase 3.2）：TE 约束下最大化预期超额。

验收标准是「样本外 TE/IR 不劣于 equal / risk_parity 基线」。本文件把它拆成：

1. **约束真的生效**：解出来的年化 TE 必须 ≤ 目标（这是硬约束，不是建议）；
2. **主动权重受控**：单只主动权重不超 ``max_active``，主动份额随 TE 目标单调；
3. **不可行时退回基准**：约束自相矛盾时返回基准权重（主动=0）并给出原因，
   绝不返回违反约束的解；
4. **样本外 IR 不劣于基线**：用「前段估参、后段评估」的框架比较
   enhanced_indexing 与 equal / risk_parity 的样本外信息比率。
"""
from __future__ import annotations

import numpy as np
import pytest

from lquant.portfolio.optimizer import (
    OptimizerError,
    active_share,
    enhanced_indexing_weight,
    tracking_error,
)
from lquant.portfolio.weighting import METHODS, min_variance_weight, risk_parity_weight, weights

D = 252


def _market(n: int = 30, t: int = 500, seed: int = 0, mu_spread: float = 0.002):
    """市场 + 特异收益 + **真实可预测的截面 α**。

    返回 ``(收益矩阵, 真协方差, 真 α)``。真 α 取 ``linspace(-spread, +spread)``：
    这样「训练段均值」是它的无偏估计（噪声 std 0.01 / sqrt(T) 远小于 spread），
    「有信息 → 该拿到正超额」这件事才是可验证的。

    之前用「特异收益的一阶自相关」当 α 是错的：那个 α 在样本外不可预测，
    任何优化器的样本外 IR 都只是掷硬币，测不出约束有没有创造价值。
    """
    rng = np.random.default_rng(seed)
    mu = np.linspace(-mu_spread, mu_spread, n)
    mkt = rng.normal(0.0, 0.012, (t, 1))
    beta = rng.normal(1.0, 0.3, (n, 1))
    spec = rng.normal(0.0, 0.010, (t, n))
    X = mu + mkt @ beta.T + spec
    return X, np.cov(X, rowvar=False), mu


def _benchmark_weights(cov: np.ndarray) -> np.ndarray:
    """基准 = 最小方差组合（比等权更接近真实指数，便于检验主动权重口径）。"""
    inv = np.linalg.pinv(cov)
    one = np.ones(cov.shape[0])
    w = inv @ one
    w = np.clip(w, 0, None)
    return w / w.sum()


# ----------------------------------------------------------- 基础口径

def test_tracking_error_matches_manual_formula():
    cov = np.array([[4e-4, 1e-4], [1e-4, 9e-4]])
    w = np.array([0.6, 0.4])
    b = np.array([0.5, 0.5])
    d = w - b
    want = np.sqrt(float(d @ cov @ d)) * np.sqrt(252)
    assert tracking_error(w, b, cov) == pytest.approx(want)


def test_active_share_definition():
    assert active_share(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == 0.0
    assert active_share(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == pytest.approx(1.0)
    # 0.5 * (0.2 + 0.2) = 0.2
    assert active_share(np.array([0.6, 0.4]), np.array([0.4, 0.6])) == pytest.approx(0.2)


def test_optimizer_respects_te_and_bounds():
    X, cov, alpha = _market()
    n = X.shape[1]
    syms = [f"s{i}" for i in range(n)]
    b = _benchmark_weights(cov)
    r = enhanced_indexing_weight(
        X, syms, benchmark_weights=dict(zip(syms, b, strict=True)),
        expected_returns=alpha, te_target=0.03, max_weight=0.15, max_active=0.02)

    assert r["_fallback"] is False, r["_fallback_reason"]
    w = np.array([r[s] for s in syms])
    assert w.sum() == pytest.approx(1.0, abs=1e-9)
    assert (w >= -1e-12).all() and (w <= 0.15 + 1e-9).all()
    # 硬约束：TE 必须满足
    assert r["_tracking_error"] <= 0.03 * 1.0001
    # 单只主动权重上限
    assert np.abs(w - b).max() <= 0.02 + 1e-6
    assert r["_active_share"] == pytest.approx(active_share(w, b), abs=1e-12)


def test_tighter_te_target_reduces_active_share():
    """TE 目标越紧，主动份额越小 —— 这是约束在起作用的最直接证据。"""
    X, cov, alpha = _market()
    n = X.shape[1]
    syms = [f"s{i}" for i in range(n)]
    b = dict(zip(syms, _benchmark_weights(cov), strict=True))
    shares = []
    for te in (0.20, 0.05, 0.01):
        r = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                     expected_returns=alpha, te_target=te,
                                     max_weight=1.0)
        assert r["_fallback"] is False
        shares.append(r["_active_share"])
    assert shares[0] >= shares[1] >= shares[2]
    assert shares[2] < shares[0]


def test_optimizer_beats_benchmark_on_expected_excess():
    """有信息时预期超额为正（否则优化器在反着做）。"""
    X, cov, alpha = _market(seed=3)
    n = X.shape[1]
    syms = [f"s{i}" for i in range(n)]
    b = dict(zip(syms, _benchmark_weights(cov), strict=True))
    r = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                 expected_returns=alpha, te_target=0.05)
    assert r["_fallback"] is False
    assert r["_expected_excess"] > 0


def test_scores_are_standardized_not_used_raw():
    """分数不是收益率：直接当 α 用会让目标函数量纲失真，必须先标准化。"""
    X, cov, alpha = _market(seed=5)
    n = X.shape[1]
    syms = [f"s{i}" for i in range(n)]
    b = dict(zip(syms, _benchmark_weights(cov), strict=True))
    scores = {s: float(alpha[i] * 1e6) for i, s in enumerate(syms)}   # 放大 1e6 倍
    r1 = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                  scores=scores, te_target=0.05)
    r2 = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                  expected_returns=alpha, te_target=0.05)
    w1 = np.array([r1[s] for s in syms])
    w2 = np.array([r2[s] for s in syms])
    # 缩放不改变优化结果（因为分数被标准化了）
    assert np.allclose(w1, w2, atol=1e-4)


def test_requires_scores_or_expected_returns():
    X, cov, _ = _market(n=5, t=60)
    syms = [f"s{i}" for i in range(5)]
    b = dict(zip(syms, np.ones(5) / 5, strict=True))
    with pytest.raises(OptimizerError, match="必须给 scores"):
        enhanced_indexing_weight(X, syms, benchmark_weights=b)


def test_rejects_bad_params():
    X, cov, alpha = _market(n=5, t=60)
    syms = [f"s{i}" for i in range(5)]
    b = dict(zip(syms, np.ones(5) / 5, strict=True))
    with pytest.raises(OptimizerError, match="te_target 必须为正"):
        enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                 expected_returns=alpha, te_target=0.0)
    with pytest.raises(OptimizerError, match="max_active 必须为正"):
        enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                 expected_returns=alpha, max_active=0.0)
    with pytest.raises(OptimizerError, match="基准权重长度"):
        enhanced_indexing_weight(X, syms, benchmark_weights=np.ones(3),
                                 expected_returns=alpha)
    with pytest.raises(OptimizerError, match="基准权重全为 0"):
        enhanced_indexing_weight(X, syms, benchmark_weights={}, expected_returns=alpha)


def test_benchmark_weights_dict_and_array_agree():
    X, cov, alpha = _market(n=8, t=200)
    syms = [f"s{i}" for i in range(8)]
    b = _benchmark_weights(cov)
    r1 = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                  expected_returns=alpha, te_target=0.05)
    r2 = enhanced_indexing_weight(X, syms, benchmark_weights=dict(zip(syms, b, strict=True)),
                                  expected_returns=alpha, te_target=0.05)
    assert np.allclose([r1[s] for s in syms], [r2[s] for s in syms], atol=1e-9)


# ----------------------------------------------------------- 不可行 → 退回基准

def test_infeasible_returns_benchmark_not_a_bad_solution():
    """TE 目标为 0 且要求主动上限 → 唯一可行解是基准本身。"""
    X, cov, alpha = _market(n=10, t=200)
    syms = [f"s{i}" for i in range(10)]
    b = _benchmark_weights(cov)
    r = enhanced_indexing_weight(X, syms, benchmark_weights=dict(zip(syms, b, strict=True)),
                                 expected_returns=alpha, te_target=1e-9)
    w = np.array([r[s] for s in syms])
    if r["_fallback"]:
        assert np.allclose(w, b, atol=1e-9)
        assert r["_fallback_reason"]
    else:
        # 也可能收敛到基准本身（主动权重≈0）
        assert np.abs(w - b).max() < 1e-4


def test_impossible_bounds_returns_benchmark():
    """``max_weight`` 小到装不下 Σw=1（如 10 只 × 上限 0.05 < 1）→ 退回基准。"""
    X, cov, alpha = _market(n=10, t=200)
    syms = [f"s{i}" for i in range(10)]
    b = _benchmark_weights(cov)
    r = enhanced_indexing_weight(X, syms, benchmark_weights=dict(zip(syms, b, strict=True)),
                                 expected_returns=alpha, max_weight=0.05)
    assert r["_fallback"] is True
    assert "矛盾" in r["_fallback_reason"]
    assert np.allclose([r[s] for s in syms], b, atol=1e-12)
    assert r["_tracking_error"] == 0.0 and r["_active_share"] == 0.0


# ----------------------------------------------------------- 权重层接线

def test_registered_in_methods_and_default_benchmark():
    """注册进 METHODS 且能被通用入口调到；缺 α 视图要报错，缺基准则用等权。"""
    assert "enhanced_indexing" in METHODS
    X, cov, alpha = _market(n=6, t=120)
    syms = [f"s{i}" for i in range(6)]
    b = dict(zip(syms, _benchmark_weights(cov), strict=True))
    # max_weight 默认 0.10 要求 n ≥ 10，否则边界与 Σw=1 矛盾、直接退回基准；
    # 6 只标的要显式放宽，否则测的其实是 fallback 分支
    w = weights(X, "enhanced_indexing", syms, benchmark_weights=b,
                expected_returns=alpha, te_target=0.05, max_weight=0.3)
    assert abs(sum(w.values()) - 1.0) < 1e-9

    # 缺 α 视图 → 明确报错（只给风险约束的话目标函数无意义），
    # 而不是静默退回等权让人以为约束生效了
    with pytest.raises(OptimizerError, match="scores"):
        weights(X, "enhanced_indexing", syms, benchmark_weights=b)

    # 缺基准 → 等权基准兜底，并在诊断里标明来源（不是真指数基准）
    r = enhanced_indexing_weight(X, syms, expected_returns=alpha, max_weight=0.3)
    assert r["_benchmark_source"] == "equal_weight_default"
    assert r["_fallback"] is False
    assert abs(sum(r.weights().values()) - 1.0) < 1e-9
    r2 = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                  expected_returns=alpha, max_weight=0.3)
    assert r2["_benchmark_source"] == "provided"


# ----------------------------------------------------------- 样本外 IR 验收

def test_out_of_sample_ir_not_worse_than_baselines():
    """验收标准：样本外 IR 不劣于 equal / risk_parity / min_variance 基线。

    框架：前 ``cut`` 期估参（协方差 + α），后段算真实超额与 IR。
    基准 = 等权市场（A 股最常见的大盘代理）。

    **跨 5 个随机种子取均值**再比较：单次样本里 risk_parity/min_variance 的
    超额是纯噪声，可能凭运气赢一次；取均值才能回答「约束有没有稳定创造价值」。
    """
    n, cut = 25, 300
    syms = [f"s{i}" for i in range(n)]
    b = np.ones(n) / n                      # 基准 = 等权市场

    def ir_of(excess: np.ndarray) -> float:
        sd = float(np.std(excess, ddof=1))
        # 主动权重恒为 0（等权 = 基准）时 IR 无定义，按惯例记 0
        return float(np.mean(excess) / sd * np.sqrt(252)) if sd > 1e-12 else 0.0

    acc: dict[str, list[float]] = {"enh": [], "rp": [], "mv": []}
    for seed in range(5):
        X, cov, _ = _market(n=n, t=500, seed=seed)
        train, test = X[:cut], X[cut:]
        # α 估计：训练段截面去均值收益（真 α 的无偏估计）
        raw = train.mean(axis=0)
        alpha_hat = raw - raw.mean()

        r = enhanced_indexing_weight(
            train, syms, benchmark_weights=dict(zip(syms, b, strict=True)),
            expected_returns=alpha_hat, te_target=0.05, max_weight=0.15,
            cov_method="shrink_lw")
        assert r["_fallback"] is False, r["_fallback_reason"]
        w_enh = np.array([r[s] for s in syms])

        w_rp = np.array([risk_parity_weight(train, syms)[s] for s in syms])
        w_mv = np.array([min_variance_weight(train, syms, cov_method="shrink_lw")[s]
                         for s in syms])
        acc["enh"].append(ir_of(test @ w_enh - test @ b))
        acc["rp"].append(ir_of(test @ w_rp - test @ b))
        acc["mv"].append(ir_of(test @ w_mv - test @ b))

    mean = {k: float(np.mean(v)) for k, v in acc.items()}
    # 等权基准的 IR 恒为 0；enhanced 必须有稳定正超额
    assert mean["enh"] > 0.3, mean
    # 不劣于两个纯风险基线（它们对 α 无感，样本外均值应接近 0）
    assert mean["enh"] > mean["rp"], mean
    assert mean["enh"] > mean["mv"], mean


# ----------------------------------------------------------- 边界与诊断

def test_result_diagnostics_and_weight_split():
    X, cov, alpha = _market(n=12, t=200)
    syms = [f"s{i}" for i in range(12)]
    r = enhanced_indexing_weight(X, syms, expected_returns=alpha, max_weight=0.2)
    diag = r.diagnostics
    assert all(k.startswith("_") for k in diag)
    assert set(r.weights()) | set(diag) == set(r)
    assert diag["_n_active"] >= 0
    assert 0.0 <= diag["_active_share"] <= 1.0


def test_optimizer_rejects_degenerate_inputs():
    X, _cov, alpha = _market(n=6, t=120)
    empty = X[:, :0]
    with pytest.raises(OptimizerError, match="没有标的"):
        enhanced_indexing_weight(empty, [], expected_returns=[])
    with pytest.raises(OptimizerError, match="te_target 必须为正"):
        enhanced_indexing_weight(X, [f"s{i}" for i in range(6)],
                                 expected_returns=alpha, te_target=0.0)
    with pytest.raises(OptimizerError, match="长度与标的数不一致"):
        enhanced_indexing_weight(X, [f"s{i}" for i in range(6)],
                                 expected_returns=alpha[:-1])
    with pytest.raises(OptimizerError, match="必须给 scores 或 expected_returns"):
        enhanced_indexing_weight(X, [f"s{i}" for i in range(6)])


def test_expected_returns_accepts_dict_and_risk_aversion_runs():
    X, _cov, alpha = _market(n=8, t=200)
    syms = [f"s{i}" for i in range(8)]
    d = dict(zip(syms, alpha, strict=True))
    r_dict = enhanced_indexing_weight(X, syms, expected_returns=d, max_weight=0.3)
    r_arr = enhanced_indexing_weight(X, syms, expected_returns=alpha, max_weight=0.3)
    assert r_dict.weights() == pytest.approx(r_arr.weights(), abs=1e-12)
    # risk_aversion > 0：目标里多一项 dᵀΣd，解必须变（否则说明这一项没生效）
    r_risk = enhanced_indexing_weight(X, syms, expected_returns=d, max_weight=0.3,
                                      risk_aversion=5.0)
    assert r_risk.weights() != pytest.approx(r_dict.weights(), abs=1e-9)


def test_optimizer_falls_back_on_zero_solution_and_te_violation(monkeypatch):
    """SLSQP 给「全零解」或「超出 TE 的解」时必须退回基准，而不是照单全收。"""
    import scipy.optimize as opt

    X, _cov, alpha = _market(n=10, t=200)
    syms = [f"s{i}" for i in range(10)]
    b = dict(zip(syms, _benchmark_weights(_cov), strict=True))

    class _Res:
        success = True

        def __init__(self, x):
            self.x = x
            self.message = "ok"

    monkeypatch.setattr(opt, "minimize", lambda *a, **k: _Res(np.zeros(10)))
    r0 = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                  expected_returns=alpha, max_weight=0.3)
    assert r0["_fallback"] is True and "全为 0" in r0["_fallback_reason"]

    huge = np.full(10, 0.5)          # 权重和 5 → 明显越界/超 TE
    monkeypatch.setattr(opt, "minimize", lambda *a, **k: _Res(huge))
    r1 = enhanced_indexing_weight(X, syms, benchmark_weights=b,
                                  expected_returns=alpha, max_weight=0.3,
                                  te_target=0.01)
    assert r1["_fallback"] is True
    assert "TE 约束" in r1["_fallback_reason"]
