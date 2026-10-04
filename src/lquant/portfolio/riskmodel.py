"""协方差估计：样本 / 收缩（LW/OAS）/ 结构化因子 / POET。

借 qlib ``model/riskmodel/`` 的**算法语义**（原生重实现，不引 qlib 运行时、
不复制其代码）。为什么组合层需要它：

A 股截面常见 ``T < N``（回看 60 天、持仓 300 只）—— 样本协方差**奇异**，
直接求逆算最小方差组合会得到「押注噪声」的极端权重。收缩与因子模型正是
为这个场景设计的：把估计往一个结构化的目标拉，用偏差换方差。

## 三个估计器各解决什么

- **收缩**（:func:`shrink_cov`）：``S_hat = (1-α)S + αF``。目标 ``F`` 是
  「常数方差」「常数相关」「单因子」之一；``α`` 由 Ledoit-Wolf 或 OAS 解析
  给出（无需交叉验证）。这是最稳的默认选择。
- **结构化**（:func:`structured_cov`）：``Σ = BΣ_F Bᵀ + diag(σ²)`` —— 前 k 个
  主成分/因子解释共同变异，残差只留对角阵。A 股里「市场 + 行业」就是前几个
  主成分，这个结构是真实的。
- **POET**（:func:`poet_cov`）：因子部分同结构化，**残差做阈值化**而不是直接
  丢成对角 —— 保留了残差里可能存在的稀疏相关。

## 关于 qlib OAS 公式的一处不一致（重要）

qlib ``shrink.py::_get_shrink_param_oas`` 的 docstring 写的是
``A = (1 - 2/p)·tr(S²) + tr(S)²``，代码却写成 ``A = (1 - 2/p)·(tr(S²) + tr(S)²)``
（把整个和乘了系数，而不是只乘第一项）。后者与 Chen et al. (2010) 的 OAS
原始公式、以及 sklearn ``covariance.oas`` 的实现都不符。

本模块按**论文/sklearn 口径**实现（与 docstring 一致），并在
:func:`oas_shrinkage` 里写明这处差异 —— 「借算法不借实现」，上游的代码
不等于上游的公式。这也正是交叉验证要发现的那类问题。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from lquant.core.errors import LQuantError

__all__ = [
    "SHRINKAGE_TARGETS", "COV_ESTIMATORS",
    "sample_cov", "shrink_cov", "structured_cov", "poet_cov", "estimate_cov",
    "oas_shrinkage", "ledoit_wolf_shrinkage",
    "condition_number", "is_psd", "nearest_psd", "ShrinkResult",
]


class RiskModelError(LQuantError):
    """协方差估计的参数/数据错误（与数据问题区分开，便于上层降级）。"""


SHRINKAGE_TARGETS = ("const_var", "const_corr", "single_factor")


# ---------------------------------------------------------------- 工具

def _matrix(returns) -> np.ndarray:
    """接受 (T,N) 数组或 polars 宽表；返回 (T,N) float64。

    NaN 一律按 0 填充 —— 与 ``portfolio/weighting.py::_returns_matrix`` 同口径
    （停牌日的收益缺失视为 0 比「整行丢弃」更符合 A 股实际：丢掉某天会把
    所有票的样本数一起改掉）。
    """
    import polars as pl

    if isinstance(returns, pl.DataFrame):
        cols = [c for c in returns.columns
                if c not in ("trade_date", "date", "symbol")]
        M = np.column_stack([returns[c].cast(pl.Float64).to_numpy() for c in cols])
    else:
        M = np.asarray(returns, dtype=float)
        if M.ndim == 1:
            M = M.reshape(-1, 1)
    return np.nan_to_num(M, nan=0.0, posinf=0.0, neginf=0.0)


def _require_2d(M: np.ndarray, *, min_rows: int = 2) -> None:
    if M.shape[0] < min_rows:
        raise RiskModelError(f"样本不足：只有 {M.shape[0]} 期，至少需要 {min_rows} 期")
    if M.shape[1] < 1:
        raise RiskModelError("没有资产列（N=0）")


# ---------------------------------------------------------------- 样本

def sample_cov(returns) -> np.ndarray:
    """样本协方差（``ddof=1``，与 ``np.cov(rowvar=False)`` 一致）。

    口径必须与仓库其它地方一致：``weighting.py`` 的 min_variance / HRP 都用
    ``np.cov`` 默认（ddof=1）。用 ddof=0 会让波动整体偏小 ``sqrt((T-1)/T)``，
    组合权重随之漂移。
    """
    M = _matrix(returns)
    _require_2d(M)
    return np.atleast_2d(np.cov(M, rowvar=False))


# ---------------------------------------------------------------- 收缩

@dataclass
class ShrinkResult:
    cov: np.ndarray
    intensity: float
    target: str
    method: str

    def __iter__(self):
        # 允许 `cov, intensity = shrink_cov(...)` 的解包用法
        return iter((self.cov, self.intensity))


def _shrink_target(M: np.ndarray, S: np.ndarray, target: str) -> np.ndarray:
    """收缩目标 F。公式与 qlib ``_get_shrink_target_*`` 一致。"""
    n = S.shape[0]
    if target == "const_var":
        # 假设同方差、零相关 → 对角阵，对角线取样本方差的均值
        F = np.eye(n)
        np.fill_diagonal(F, float(np.mean(np.diag(S))))
        return F
    if target == "const_corr":
        # 假设相关系数相同但方差保留 → r_bar * sqrt(vi*vj)
        var = np.diag(S).copy()
        sqrt_var = np.sqrt(np.clip(var, 1e-300, None))
        covar = np.outer(sqrt_var, sqrt_var)
        with np.errstate(divide="ignore", invalid="ignore"):
            r_bar = (np.sum(S / covar) - n) / (n * (n - 1))
        r_bar = float(np.nan_to_num(r_bar, nan=0.0))
        F = r_bar * covar
        np.fill_diagonal(F, var)
        return F
    if target == "single_factor":
        # 单因子（市场）模型：F_ij = cov_i,mkt * cov_j,mkt / var_mkt
        x_mkt = np.nanmean(M, axis=1)
        cov_mkt = np.asarray(M.T.dot(x_mkt) / len(M))
        var_mkt = float(np.asarray(x_mkt.dot(x_mkt) / len(M)))
        if var_mkt <= 1e-300:
            raise RiskModelError("单因子目标不可用：市场因子方差为 0")
        F = np.outer(cov_mkt, cov_mkt) / var_mkt
        np.fill_diagonal(F, np.diag(S))
        return F
    raise RiskModelError(f"未知收缩目标 {target!r}，可选: {list(SHRINKAGE_TARGETS)}")


def ledoit_wolf_shrinkage(M: np.ndarray, S: np.ndarray, F: np.ndarray,
                          target: str) -> float:
    """Ledoit-Wolf 最优收缩强度（解析解，无需交叉验证）。

    三个目标的公式不同（常数方差 / 常数相关 / 单因子），与 qlib 一致。
    """
    t, n = M.shape
    y = M ** 2
    gamma = float(np.linalg.norm(S - F, "fro") ** 2)
    if gamma <= 1e-300:
        return 0.0
    if target == "const_var":
        phi = float(np.sum(y.T.dot(y) / t - S ** 2))
        kappa = phi / gamma
    elif target == "const_corr":
        var = np.diag(S)
        sqrt_var = np.sqrt(np.clip(var, 1e-300, None))
        with np.errstate(divide="ignore", invalid="ignore"):
            r_bar = (np.sum(S / np.outer(sqrt_var, sqrt_var)) - n) / (n * (n - 1))
        r_bar = float(np.nan_to_num(r_bar, nan=0.0))
        phi_mat = y.T.dot(y) / t - S ** 2
        phi = float(np.sum(phi_mat))
        theta_mat = (M ** 3).T.dot(M) / t - var[:, None] * S
        np.fill_diagonal(theta_mat, 0.0)
        rho = float(np.sum(np.diag(phi_mat))
                    + r_bar * np.sum(np.outer(1 / sqrt_var, sqrt_var) * theta_mat))
        kappa = (phi - rho) / gamma
    elif target == "single_factor":
        x_mkt = np.nanmean(M, axis=1)
        cov_mkt = np.asarray(M.T.dot(x_mkt) / len(M))
        var_mkt = float(np.asarray(x_mkt.dot(x_mkt) / len(M)))
        if var_mkt <= 1e-300:
            return 0.0
        phi_mat = y.T.dot(y) / t - S ** 2
        phi = float(np.sum(phi_mat))
        theta_mat = (M ** 3).T.dot(M) / t - np.diag(S)[:, None] * S
        np.fill_diagonal(theta_mat, 0.0)
        rho = float(np.sum(np.diag(phi_mat))
                    + np.sum(cov_mkt[:, None] * theta_mat) / var_mkt)
        kappa = (phi - rho) / gamma
    else:
        raise RiskModelError(f"未知收缩目标 {target!r}")
    return float(min(1.0, max(0.0, kappa / t)))


def oas_shrinkage(M: np.ndarray, S: np.ndarray | None = None) -> float:
    """Oracle Approximating Shrinkage（Chen et al. 2010，向 ``μI`` 收缩）。

        A = (1 - 2/p)·tr(S²) + tr(S)²
        B = (n + 1 - 2/p)·(tr(S²) - tr(S)²/p)
        α = min(1, A / B)

    ``S`` 用 **MLE（ddof=0）** 口径 —— 论文与 sklearn 都基于 MLE 推导，
    用 ddof=1 会让 α 偏小（收缩不足）。这是本函数与 ``sample_cov`` 口径
    不同的**唯一**地方，特此写明。

    注意与 qlib 的差异：qlib 代码把 ``A`` 写成 ``(1-2/p)·(tr(S²) + tr(S)²)``
    （整和乘系数），与它自己的 docstring 及原始论文都不符。这里按论文实现。
    """
    t, p = M.shape
    if p < 1:
        raise RiskModelError("OAS 需要至少 1 个资产")
    Sm = np.atleast_2d(np.cov(M, rowvar=False, ddof=0)) if S is None else S
    tr_s2 = float(np.sum(Sm ** 2))
    tr2_s = float(np.trace(Sm) ** 2)
    a = (1.0 - 2.0 / p) * tr_s2 + tr2_s
    b = (t + 1.0 - 2.0 / p) * (tr_s2 - tr2_s / p)
    if abs(b) <= 1e-300:
        return 0.0
    return float(min(1.0, max(0.0, a / b)))


def shrink_cov(returns, *, shrinkage: str = "lw", target: str = "const_var",
               alpha: float | None = None) -> ShrinkResult:
    """收缩协方差：``S_hat = (1-α)S + αF``。

    Parameters
    ----------
    shrinkage : ``lw``（Ledoit-Wolf 解析解）/ ``oas``（仅支持 const_var 目标）/
        或直接给 ``alpha``（0~1 手调）。
    target : 收缩目标，见 :data:`SHRINKAGE_TARGETS`。
    alpha : 显式指定收缩强度；给了就忽略 ``shrinkage`` 的估计器。

    返回 :class:`ShrinkResult`（可解包成 ``(cov, intensity)``）。
    """
    M = _matrix(returns)
    _require_2d(M)
    if target not in SHRINKAGE_TARGETS:
        raise RiskModelError(f"未知收缩目标 {target!r}，可选: {list(SHRINKAGE_TARGETS)}")
    if shrinkage == "oas" and target != "const_var":
        # 与 qlib 的限制一致：OAS 的推导只对「向 μI 收缩」成立
        raise RiskModelError("OAS 只支持 target='const_var'（向 μI 收缩）")
    if shrinkage not in ("lw", "oas") and alpha is None:
        raise RiskModelError(f"未知收缩估计器 {shrinkage!r}（可选 lw/oas，或直接给 alpha）")

    S = np.atleast_2d(np.cov(M, rowvar=False))
    F = _shrink_target(M, S, target)
    if alpha is None:
        a = (oas_shrinkage(M) if shrinkage == "oas"
             else ledoit_wolf_shrinkage(M, S, F, target))
    else:
        a = float(alpha)
        if not (0.0 <= a <= 1.0):
            raise RiskModelError(f"alpha 必须在 [0,1]，收到 {a}")
    out = (1.0 - a) * S + a * F
    return ShrinkResult(cov=_symmetrize(out), intensity=a,
                        target=target, method=shrinkage if alpha is None else "fixed")


# ---------------------------------------------------------------- 结构化 / POET

def _pca_factors(M: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """PCA 因子分解：返回 ``(B, F, Mc)``。

    - ``B``  (N,k)：**正交归一**载荷（右奇异向量的前 k 列）
    - ``F``  (T,k)：因子得分 ``= Mc @ B``
    - ``Mc`` (T,N)：去均值后的收益

    满足 ``Mc ≈ F @ Bᵀ``。用 SVD 而不是 ``eig(cov)``：条件数大时更稳，
    且天然按奇异值降序给出「最重要的 k 个方向」。
    """
    Mc = M - M.mean(axis=0, keepdims=True)
    _, sv, vt = np.linalg.svd(Mc, full_matrices=False)
    k = min(k, len(sv))
    B = vt[:k].T
    F = Mc @ B
    return B, F, Mc


def _fa_factors(M: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """因子分析分解；sklearn 不可用/不收敛时降级为 PCA（调用方不需要知道）。"""
    try:
        from sklearn.decomposition import FactorAnalysis

        fa = FactorAnalysis(n_components=k, random_state=0)
        F = fa.fit_transform(M)
        B = fa.components_.T
        recon = F @ B.T + fa.mean_
        return B, F, M - recon
    except Exception:  # noqa: BLE001
        B, F, Mc = _pca_factors(M, k)
        return B, F, Mc - F @ B.T


def structured_cov(returns, *, n_factors: int = 10, method: str = "pca") -> np.ndarray:
    """结构化因子协方差：``Σ = B Σ_F Bᵀ + diag(σ²)``。

    ``method="pca"``：对收益做主成分，前 k 个成分作因子载荷；
    ``method="fa"``：因子分析（需要 sklearn，缺失时降级为 PCA）。

    残差**只保留对角**（特异风险）—— 这是「结构化」的定义，也是它比样本
    协方差稳的原因：不把 N² 个噪声相关一起估出来。
    """
    M = _matrix(returns)
    _require_2d(M)
    n = M.shape[1]
    k = int(n_factors)
    if k < 1:
        raise RiskModelError("n_factors 必须 ≥ 1")
    k = min(k, n, max(M.shape[0] - 1, 1))
    if method not in ("pca", "fa"):
        raise RiskModelError(f"未知因子模型 {method!r}（可选 pca/fa）")

    if method == "fa":
        B, F, resid = _fa_factors(M, k)
    else:
        B, F, Mc = _pca_factors(M, k)
        resid = Mc - F @ B.T

    cov_f = _cov(F)
    common = B @ cov_f @ B.T
    spec = np.var(resid, axis=0, ddof=1) if resid.shape[0] > 1 else np.zeros(n)
    return _symmetrize(common + np.diag(np.clip(spec, 0.0, None)))


def _cov(x: np.ndarray) -> np.ndarray:
    """(T,k) → (k,k) 协方差；k=1 时 np.cov 返回 0 维，统一成 2 维。"""
    c = np.atleast_2d(np.cov(x, rowvar=False))
    return c.reshape(1, 1) if c.size == 1 else c


def poet_cov(returns, *, n_factors: int = 10, threshold: float | None = None,
             method: str = "soft") -> np.ndarray:
    """POET：因子部分同结构化，**残差做阈值化**（而非丢成对角）。

    阈值默认取 Fan et al. (2013) 的理论率 ``sqrt(log p / n)`` 乘以残差
    标准差量级 —— 直接照搬 qlib 的固定 ``thresh=1.0`` 在不同量纲下没有意义
    （收益是小数，1.0 相当于几乎不阈值化）。

    ``method``：``soft``（默认，连续收缩）/ ``hard``（硬截断）。
    """
    M = _matrix(returns)
    _require_2d(M)
    p = M.shape[1]
    t = M.shape[0]
    k = int(n_factors)
    if k < 1:
        raise RiskModelError("n_factors 必须 ≥ 1")
    k = min(k, p, max(t - 1, 1))
    if method not in ("soft", "hard"):
        raise RiskModelError(f"未知阈值化方法 {method!r}（可选 soft/hard）")

    B, F, Mc = _pca_factors(M, k)
    resid = Mc - F @ B.T
    common = B @ _cov(F) @ B.T

    emp = _cov(resid)
    if threshold is None:
        rate = np.sqrt(np.log(max(p, 2)) / max(t, 2))
        scale = float(np.sqrt(np.mean(np.diag(emp)))) if p else 0.0
        thr = float(rate * scale)
    else:
        thr = float(threshold)
    if method == "soft":
        th = np.sign(emp) * np.clip(np.abs(emp) - thr, 0.0, None)
    else:
        th = np.where(np.abs(emp) > thr, emp, 0.0)
    # 对角保留原特异方差（阈值化不该把自身方差也砍掉）
    np.fill_diagonal(th, np.diag(emp))
    return _symmetrize(common + th)


def _symmetrize(c: np.ndarray) -> np.ndarray:
    """对称化 + 保证对角为正（数值误差会破坏对称性，进而让优化器不稳）。"""
    c = (c + c.T) / 2.0
    np.fill_diagonal(c, np.clip(np.diag(c), 0.0, None))
    return c


# ---------------------------------------------------------------- 注册表 + 诊断

def estimate_cov(returns, method: str = "shrink_lw", **params) -> np.ndarray:
    """统一入口。``method`` 见 :data:`COV_ESTIMATORS`。"""
    if method not in COV_ESTIMATORS:
        raise RiskModelError(
            f"未知协方差估计器 {method!r}，可选: {sorted(COV_ESTIMATORS)}")
    out = COV_ESTIMATORS[method](returns, **params)
    return out.cov if isinstance(out, ShrinkResult) else out


def condition_number(cov: np.ndarray) -> float:
    """条件数（最大/最小特征值）。越小越稳；``inf`` = 奇异。

    这是「要不要用收缩」的直接判据：样本协方差在 T<N 时条件数会大到
    1e16 量级，此时最小方差组合的权重由数值噪声决定。
    """
    c = np.atleast_2d(np.asarray(cov, dtype=float))
    c = (c + c.T) / 2.0
    ev = np.linalg.eigvalsh(c)
    lo = float(ev.min())
    hi = float(ev.max())
    if lo <= 0:
        return float("inf")
    return hi / lo


def is_psd(cov: np.ndarray, tol: float = 1e-10) -> bool:
    """半正定判定（对称 + 最小特征值 ≥ -tol）。"""
    c = np.atleast_2d(np.asarray(cov, dtype=float))
    if not np.allclose(c, c.T, atol=1e-12):
        return False
    return bool(np.linalg.eigvalsh((c + c.T) / 2.0).min() >= -abs(tol))


def nearest_psd(cov: np.ndarray, *, eps: float = 1e-12) -> np.ndarray:
    """最近半正定修正：特征值截断到 ``eps`` 后重建。

    收缩/因子估计理论上 PSD，但浮点误差可能产生 -1e-18 的负特征值；
    直接送进 SLSQP 会因为非凸性报「未收敛」，而根因只是一个舍入误差。
    """
    c = np.atleast_2d(np.asarray(cov, dtype=float))
    c = (c + c.T) / 2.0
    ev, vec = np.linalg.eigh(c)
    ev = np.clip(ev, eps, None)
    return _symmetrize((vec * ev) @ vec.T)


COV_ESTIMATORS: dict[str, Any] = {
    "sample": sample_cov,
    "shrink_lw": lambda r, **kw: shrink_cov(r, shrinkage="lw", **kw),
    "shrink_oas": lambda r, **kw: shrink_cov(r, shrinkage="oas", **kw),
    "structured_pca": lambda r, **kw: structured_cov(r, method="pca", **kw),
    "structured_fa": lambda r, **kw: structured_cov(r, method="fa", **kw),
    "poet": poet_cov,
}
