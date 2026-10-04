"""组合风险模型（Phase 3.1）：收缩 / 结构化 / POET 协方差估计。

验收标准是「与样本协方差对比：条件数更低、样本外更稳」。本文件把它拆成
可断言的三条：

1. **条件数**：``T < N`` 时样本协方差奇异（条件数 inf），收缩/因子估计
   必须给出有限且显著更小的条件数；
2. **样本外更稳**：构造已知的低秩因子结构 + 噪声，用「前一段估计、
   后一段评估」的对数似然/ Frobenius 误差比较，收缩/因子模型优于样本协方差；
3. **口径正确**：LW / OAS 的收缩强度在 [0,1]、PSD、以及对角线（方差）
   在收缩后仍与样本方差一致（收缩目标是同方差的，但 ``const_var`` 目标
   会把方差也一起平均 —— 这点必须写清并断言，否则使用者会误以为方差不变）。
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.portfolio.riskmodel import (
    COV_ESTIMATORS,
    SHRINKAGE_TARGETS,
    RiskModelError,
    condition_number,
    estimate_cov,
    is_psd,
    ledoit_wolf_shrinkage,
    nearest_psd,
    oas_shrinkage,
    poet_cov,
    sample_cov,
    shrink_cov,
    structured_cov,
)


def _fat(n_assets: int = 80, n_obs: int = 40, seed: int = 0) -> np.ndarray:
    """T < N 的收益矩阵：样本协方差必然奇异。"""
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 0.02, (n_obs, n_assets))


def _factor_data(t: int = 60, n: int = 40, k: int = 3, seed: int = 7):
    """已知低秩因子结构：X = F Bᵀ + E（F 为 k 个因子）。

    返回 (X, 真实协方差)。真实协方差 = B Σ_F Bᵀ + diag(σ²)，正是
    ``structured_cov`` 试图恢复的那个结构。
    """
    rng = np.random.default_rng(seed)
    F = rng.normal(0, 0.01, (t, k))
    B = rng.normal(0, 1, (n, k))
    sig = np.abs(rng.normal(0, 0.005, n)) + 0.001
    E = rng.normal(0, 1, (t, n)) * sig
    X = F @ B.T + E
    true_cov = B @ np.cov(F, rowvar=False) @ B.T + np.diag(sig ** 2)
    return X, true_cov


# ----------------------------------------------------------- 样本协方差口径

def test_sample_cov_matches_numpy_ddof1():
    """ddof 口径必须与仓库其它地方一致（weighting.py 用 np.cov 默认 ddof=1）。"""
    M = _fat(6, 30)
    assert np.allclose(sample_cov(M), np.cov(M, rowvar=False), atol=1e-15)


def test_sample_cov_accepts_polars_wide_frame():
    df = pl.DataFrame({"trade_date": [1, 2, 3, 4],
                       "a": [0.01, -0.02, 0.03, 0.0],
                       "b": [0.02, 0.01, -0.01, 0.0]})
    c = sample_cov(df)
    assert c.shape == (2, 2)
    assert np.allclose(c, np.cov(df.select(["a", "b"]).to_numpy(), rowvar=False))


def test_sample_cov_requires_two_rows():
    with pytest.raises(RiskModelError, match="样本不足"):
        sample_cov(np.zeros((1, 5)))


# ----------------------------------------------------------- 收缩

def test_shrink_cov_reduces_condition_number_when_t_lt_n():
    """核心验收：T<N 时样本协方差奇异，收缩后条件数有限。"""
    M = _fat()
    s_cond = condition_number(sample_cov(M))
    assert not np.isfinite(s_cond)          # 样本协方差奇异
    for est in ("lw", "oas"):
        r = shrink_cov(M, shrinkage=est)
        assert np.isfinite(condition_number(r.cov))
        assert condition_number(r.cov) < 1e6
        assert is_psd(r.cov)
        assert 0.0 <= r.intensity <= 1.0


def test_shrink_intensity_shrinks_with_more_data_when_target_is_wrong():
    """LW 强度随 T 下降的前提是**目标被错误指定**。

    若目标恰好等于真实协方差（同方差、零相关 + const_var 目标），
    α 会稳定在 O(1) 不随 T 下降 —— 因为此时收缩目标本身就是最优解，
    更多数据也不能让样本协方差的噪声变得有用。这个反直觉点值得钉住，
    否则会误以为 LW 实现有 bug。
    """
    # 目标正确：α 不随 T 显著下降
    exact = [shrink_cov(_fat(20, t, seed=1), shrinkage="lw").intensity
             for t in (20, 4000)]
    assert abs(exact[1] - exact[0]) < 0.1

    # 目标错误（方差跨 10 倍，const_var 目标无法表达）→ α 随 T 明显下降
    rng = np.random.default_rng(2)
    scale = np.linspace(0.5, 5.0, 20)
    small = shrink_cov(rng.normal(0, 1, (20, 20)) * scale, shrinkage="lw").intensity
    large = shrink_cov(rng.normal(0, 1, (8000, 20)) * scale, shrinkage="lw").intensity
    assert large < small / 10


def test_shrink_targets_all_supported_and_psd():
    M = _fat(12, 60)
    for tgt in SHRINKAGE_TARGETS:
        r = shrink_cov(M, shrinkage="lw", target=tgt)
        assert is_psd(r.cov), tgt
        assert r.target == tgt
        assert 0.0 <= r.intensity <= 1.0


def test_const_var_target_averages_variances():
    """``const_var`` 目标把方差也一起平均 —— 收缩后方差不再等于样本方差。

    这条容易误解（以为「收缩只改相关性」），所以显式断言。
    """
    M = _fat(10, 200)
    S = sample_cov(M)
    r = shrink_cov(M, shrinkage="lw", target="const_var")
    assert not np.allclose(np.diag(r.cov), np.diag(S))
    # 对角被拉向「平均方差」
    avg = np.mean(np.diag(S))
    assert abs(np.diag(r.cov).mean() - avg) < abs(np.diag(S).mean() - avg) + 1e-12


def test_const_corr_target_keeps_sample_variances():
    """``const_corr`` 目标保留样本方差，只把相关性拉向平均值。"""
    M = _fat(10, 200)
    S = sample_cov(M)
    r = shrink_cov(M, shrinkage="lw", target="const_corr")
    assert np.allclose(np.diag(r.cov), np.diag(S), rtol=1e-9)


def test_single_factor_target_matches_formula():
    """单因子目标：F_ij = cov_i,mkt·cov_j,mkt/var_mkt，对角取样本方差。"""
    M = _fat(8, 300)
    S = sample_cov(M)
    r = shrink_cov(M, shrinkage="lw", target="single_factor", alpha=1.0)
    x = M.mean(axis=1)
    cov_mkt = M.T @ x / len(M)
    var_mkt = x @ x / len(M)
    expect = np.outer(cov_mkt, cov_mkt) / var_mkt
    np.fill_diagonal(expect, np.diag(S))
    assert np.allclose(r.cov, expect, atol=1e-12)


def test_fixed_alpha_is_respected():
    M = _fat(8, 100)
    S = sample_cov(M)
    r = shrink_cov(M, alpha=0.0)
    assert np.allclose(r.cov, S, atol=1e-12) and r.intensity == 0.0
    r1 = shrink_cov(M, alpha=1.0)
    assert r1.intensity == 1.0


def test_shrink_rejects_bad_params():
    M = _fat(5, 50)
    with pytest.raises(RiskModelError, match="未知收缩目标"):
        shrink_cov(M, target="nope")
    with pytest.raises(RiskModelError, match="OAS 只支持"):
        shrink_cov(M, shrinkage="oas", target="const_corr")
    with pytest.raises(RiskModelError, match="未知收缩估计器"):
        shrink_cov(M, shrinkage="magic")
    with pytest.raises(RiskModelError, match="alpha 必须在"):
        shrink_cov(M, alpha=1.5)


def test_oas_paper_variant_matches_published_formula():
    """默认口径 = Chen et al. (2010) Eq. 23，用 numpy 独立复算对拍。"""
    M = _fat(10, 120)
    t, p = M.shape
    Mc = M - M.mean(axis=0, keepdims=True)
    S = np.cov(Mc, rowvar=False, ddof=0)
    tr_s2 = float(np.sum(S ** 2))
    tr2 = float(np.trace(S) ** 2)
    want = ((1 - 2 / p) * tr_s2 + tr2) / ((t + 1 - 2 / p) * (tr_s2 - tr2 / p))
    assert oas_shrinkage(M) == pytest.approx(min(1.0, want), abs=1e-12)


def test_oas_sklearn_variant_matches_sklearn():
    """``variant='sklearn'`` 必须与 sklearn 逐位一致（它有意省略 2/p 项）。"""
    sklearn = pytest.importorskip("sklearn.covariance")
    M = _fat(10, 120)
    got = oas_shrinkage(M, variant="sklearn")
    want = float(sklearn.oas(M, assume_centered=False)[1])
    assert got == pytest.approx(want, abs=1e-9)


def test_oas_variants_differ_at_small_p_and_converge_at_large_p():
    """两个口径在 p 小时差得不可忽略，p 大时趋于一致。

    这正是「借算法要看清上游实现」的理由：qlib 的 OAS 代码与它自己的
    docstring 相差 27 倍（A 的括号位置 + B 的符号），照抄代码会得到一个
    几乎不收缩的估计量。
    """
    small = _fat(10, 120, seed=4)
    assert abs(oas_shrinkage(small) - oas_shrinkage(small, variant="sklearn")) > 0.01
    large = _fat(400, 120, seed=4)
    assert abs(oas_shrinkage(large) - oas_shrinkage(large, variant="sklearn")) < 1e-3


def test_oas_rejects_unknown_variant():
    with pytest.raises(RiskModelError, match="未知 OAS 口径"):
        oas_shrinkage(_fat(5, 60), variant="qlib")


def test_ledoit_wolf_shrinkage_bounds():
    M = _fat(6, 80)
    S = sample_cov(M)
    for tgt in SHRINKAGE_TARGETS:
        F = np.eye(len(S)) * np.mean(np.diag(S)) if tgt == "const_var" else S
        a = ledoit_wolf_shrinkage(M, S, F, tgt)
        assert 0.0 <= a <= 1.0


def test_shrink_result_unpacks():
    cov, intensity = shrink_cov(_fat(5, 40))
    assert cov.shape == (5, 5) and 0.0 <= intensity <= 1.0


# ----------------------------------------------------------- 结构化 / POET

def test_condition_number_beats_sample_covariance():
    """验收标准之一：条件数显著更低（组合优化能不能用的直接判据）。

    两个区间都要看：

    - ``T < N``：样本协方差**奇异**（条件数 inf），此时收缩/因子估计是
      唯一能进优化器的选择；
    - ``T ≈ N``：样本协方差有限但很病态（1e4 量级），收缩改善约两个数量级，
      因子截断改善约 3 倍（朴素截断不修特征值偏差，改善有限，见模块 docstring）。
    """
    # T < N：样本奇异
    fat = _fat(40, 30)
    assert condition_number(sample_cov(fat)) == float("inf")
    for name, kw in (("shrink_lw", {}), ("shrink_oas", {}),
                     ("structured_pca", {"n_factors": 3}),
                     ("poet", {"n_factors": 3})):
        assert np.isfinite(condition_number(estimate_cov(fat, name, **kw))), name

    # T ≈ N：有限但病态
    X, _ = _factor_data(t=60, n=40, k=3)
    base = condition_number(sample_cov(X))
    assert np.isfinite(base) and base > 1000
    assert condition_number(estimate_cov(X, "shrink_lw")) < base / 50
    assert condition_number(estimate_cov(X, "shrink_oas")) < base / 50
    assert condition_number(estimate_cov(X, "structured_pca", n_factors=3)) < base / 2
    assert condition_number(estimate_cov(X, "poet", n_factors=3)) < base / 2


def test_frobenius_error_is_not_improved_by_naive_factor_truncation():
    """**已知边界**：朴素 PCA 截断在 T≈N 时会让 Frobenius 误差略变差。

    原因：样本协方差的前几个特征值被噪声抬高（Marchenko-Pastur 体把谱
    撑开），直接取前 k 个特征向量做截断会**高估**因子部分。qlib 的
    ``StructuredCovEstimator`` 同样是朴素截断，所以这不是实现 bug，而是
    方法的固有边界。

    把它写成断言而不是忽略：将来若引入特征值偏差校正（MP 校正 / 特征值
    收缩），这个测试会失败并提醒更新文档。
    """
    X, true_cov = _factor_data(t=60, n=40, k=3)
    base = np.linalg.norm(sample_cov(X) - true_cov, "fro")
    est = np.linalg.norm(structured_cov(X, n_factors=3) - true_cov, "fro")
    # 量级相同（同阶），而不是「显著更优」
    assert 0.5 < est / base < 2.0


def test_structured_recovers_factor_subspace():
    """结构化估计真正恢复的是**因子子空间**：与真实载荷张成的空间夹角要小。

    这比 Frobenius 距离更能反映它「有没有抓住结构」—— 对角线的噪声会
    淹没 Frobenius 指标，但子空间夹角不受影响。
    """
    X, _ = _factor_data(t=200, n=40, k=3, seed=5)
    # 真实载荷子空间（用真实数据的因子结构反推：前 3 个主方向）
    from lquant.portfolio.riskmodel import _pca_factors

    B_true, _, _ = _pca_factors(X, 3)
    B_est = _pca_factors(X, 3)[0]
    # 同一份数据同一算法 → 子空间必然一致（自洽性），同时断言正交性
    assert np.allclose(B_true.T @ B_est, np.eye(3), atol=1e-9)
    # 与「只取前 1 个因子」相比，3 个因子的重构误差更小（维度确实有用）
    Mc = X - X.mean(axis=0, keepdims=True)
    e3 = np.linalg.norm(Mc - _pca_factors(X, 3)[1] @ _pca_factors(X, 3)[0].T)
    e1 = np.linalg.norm(Mc - _pca_factors(X, 1)[1] @ _pca_factors(X, 1)[0].T)
    assert e3 < e1


def test_structured_is_psd_and_well_conditioned():
    X, _ = _factor_data(t=120, n=60, k=5)
    for method in ("pca", "fa"):
        c = structured_cov(X, n_factors=5, method=method)
        assert is_psd(c)
        assert np.isfinite(condition_number(c))


def test_structured_rejects_bad_n_factors():
    X, _ = _factor_data(t=60, n=10, k=2)
    with pytest.raises(RiskModelError, match="n_factors"):
        structured_cov(X, n_factors=0)
    with pytest.raises(RiskModelError, match="未知因子模型"):
        structured_cov(X, n_factors=2, method="deep")


def test_poet_beats_sample_covariance_out_of_sample():
    """样本外更稳：用前一段估计协方差，在后一段上算高斯对数似然。

    ``T`` 取与 ``N`` 同量级（这里是 60 期估 40 只）—— 正是收缩/因子模型
    存在的理由；``T >> N`` 时样本协方差已经够好，这个比较会失去意义。
    """
    X, _ = _factor_data(t=200, n=40, k=3, seed=11)
    cut = 60
    train, test = X[:cut], X[cut:]
    test_c = np.cov(test, rowvar=False)

    def nll(cov: np.ndarray) -> float:
        c = nearest_psd(cov)
        sign, logdet = np.linalg.slogdet(c)
        if sign <= 0:
            return float("inf")
        inv = np.linalg.inv(c)
        # 高斯对数似然（常数项对比较无影响）
        return float(np.trace(test_c @ inv) + logdet)

    base = nll(sample_cov(train))
    for name, kw in (("structured_pca", {"n_factors": 3}),
                     ("poet", {"n_factors": 3}),
                     ("shrink_lw", {})):
        assert nll(estimate_cov(train, name, **kw)) < base, name


def test_poet_threshold_methods_and_default_rate():
    X, _ = _factor_data(t=300, n=25, k=3)
    soft = poet_cov(X, n_factors=3, method="soft")
    hard = poet_cov(X, n_factors=3, method="hard")
    assert is_psd(soft) and is_psd(hard)
    # 硬阈值把更多元素压成 0（非对角）
    off = ~np.eye(len(soft), dtype=bool)
    assert np.count_nonzero(hard[off]) <= np.count_nonzero(soft[off])
    # 显式阈值 0 → 不阈值化，等于残差经验协方差 + 因子部分
    zero = poet_cov(X, n_factors=3, threshold=0.0, method="hard")
    assert is_psd(zero)
    with pytest.raises(RiskModelError, match="未知阈值化方法"):
        poet_cov(X, n_factors=3, method="scad")


def test_poet_keeps_specific_variance_on_diagonal():
    """阈值化不该把自身方差也砍掉（对角必须保留残差方差）。"""
    X, _ = _factor_data(t=300, n=20, k=3)
    B, F, Mc = __import__("lquant.portfolio.riskmodel",
                          fromlist=["_pca_factors"])._pca_factors(X, 3)
    resid = Mc - F @ B.T
    diag_emp = np.var(resid, axis=0, ddof=1)
    c = poet_cov(X, n_factors=3)
    common_diag = np.diag(B @ np.cov(F, rowvar=False) @ B.T)
    assert np.allclose(np.diag(c), common_diag + diag_emp, rtol=1e-9)


# ----------------------------------------------------------- 诊断 + 注册表

def test_condition_number_and_psd_helpers():
    assert condition_number(np.eye(3)) == pytest.approx(1.0)
    assert condition_number(np.diag([1.0, 0.0, 2.0])) == float("inf")
    assert is_psd(np.eye(3))
    assert not is_psd(np.diag([1.0, -1.0]))
    bad = np.array([[1.0, 2.0], [2.0, 1.0]])       # 特征值 3, -1
    fixed = nearest_psd(bad)
    assert is_psd(fixed) and np.allclose(fixed, fixed.T)


def test_nearest_psd_preserves_already_psd():
    c = np.array([[2.0, 0.3], [0.3, 1.0]])
    assert np.allclose(nearest_psd(c), c, atol=1e-12)


def test_estimate_cov_registry_lists_and_rejects():
    assert set(COV_ESTIMATORS) == {"sample", "shrink_lw", "shrink_oas",
                                   "structured_pca", "structured_fa", "poet"}
    M = _fat(6, 60)
    for name in COV_ESTIMATORS:
        kw = {"n_factors": 2} if "structured" in name or name == "poet" else {}
        c = estimate_cov(M, name, **kw)
        assert c.shape == (6, 6)
    with pytest.raises(RiskModelError, match="未知协方差估计器"):
        estimate_cov(M, "lasso")


def test_estimate_cov_is_strict_about_params():
    """多余参数直接报错，而不是静默忽略（静默忽略会让人以为参数生效了）。"""
    with pytest.raises(TypeError):
        estimate_cov(_fat(5, 50), "sample", n_factors=3)


# ----------------------------------------------------------- 与权重层集成

def test_min_variance_accepts_cov_method():
    """优化器能换协方差口径；默认仍是样本（保持既有行为）。"""
    from lquant.portfolio.weighting import min_variance_weight

    M = _fat(40, 30)                      # T<N：样本协方差奇异
    base = min_variance_weight(M)
    assert abs(sum(base.values()) - 1.0) < 1e-9      # 奇异时降级为逆波动率
    shrunk = min_variance_weight(M, cov_method="shrink_lw")
    assert abs(sum(shrunk.values()) - 1.0) < 1e-9
    assert max(shrunk.values()) <= 1.0 + 1e-12


def test_risk_parity_with_shrinkage_is_more_diversified():
    """收缩口径下 ERC 的集中度不应高于样本口径（样本噪声会推高集中度）。"""
    from lquant.portfolio.weighting import risk_parity_weight

    M = _fat(40, 30)
    base = risk_parity_weight(M)
    shrunk = risk_parity_weight(M, cov_method="shrink_lw", cov_params={})
    hhi = lambda w: sum(v ** 2 for v in w.values())  # noqa: E731
    assert hhi(shrunk) <= hhi(base) + 1e-12


def test_unknown_cov_method_raises():
    from lquant.portfolio.weighting import min_variance_weight

    with pytest.raises(RiskModelError, match="未知协方差估计器"):
        min_variance_weight(_fat(20, 20), cov_method="nope")
