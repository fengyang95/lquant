"""回测统计置信度：PSR / DSR / CSCV-PBO + 试验台账。

借鉴 López de Prado 方法论的开源生态（fluke / pypbo / ml4t-diagnostic）。
公式本体小到内联即可，不值得为此引入新依赖 —— numpy + stdlib math +
既有依赖 scipy.stats（偏度/峰度）。

lquant 是多重检验的高危用户：GP/LLM 挖掘一轮几百候选、sweep 万级网格。
「回测很美，上线就废」的根源就是只报告观测 Sharpe、不报告它有多少是运气：

- **PSR**（Probabilistic Sharpe Ratio）：给定观测夏普，真实夏普高于基准
  的概率 —— 偏度/峰度校正后的显著性检验。
- **E[maxSR] / DSR**（Deflated Sharpe Ratio）：试过 N 个配置后，期望的
  「最大噪声夏普」水位是多少；把观测夏普相对这个水位检验。
- **CSCV-PBO**（Combinatorially-Symmetric CV, Probability of Backtest
  Overfitting）：把样本切成偶数块、枚举全部一半/一半组合，看「样本内
  排名最优的配置在样本外跌出中位」的频率 —— 直接度量选参即过拟合。
- **试验台账**：n_trials 必须统计**所有**试过的配置（含放弃的），
  只数活下来的等于给橡皮图章盖章。台账入口见 ``backtest/runs.count_runs``。

口径约定：所有夏普参数（除 ``sharpe_ratio`` 的返回值外）一律**日频**
（未年化），避免 √freq 到处乱飞；文档里写清楚的就是代码里做的。
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np

__all__ = [
    "sharpe_ratio",
    "psr",
    "expected_max_sharpe",
    "deflated_sharpe",
    "cscv_pbo",
]

_EULER_GAMMA = 0.5772156649015329


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    from statistics import NormalDist

    if not 0.0 < p < 1.0:
        raise ValueError(f"分位数 p 须在 (0,1)，收到 {p}")
    return NormalDist().inv_cdf(p)


def _as_returns(x) -> np.ndarray:
    r = np.asarray(x, dtype=float).ravel()
    if not len(r):
        raise ValueError("收益序列为空")
    if not np.all(np.isfinite(r)):
        # 静默丢非有限值会改变统计口径 —— 谁的数据谁清洗
        raise ValueError("收益序列含 NaN/Inf，先清洗再算置信度")
    return r


def _sr_moments(r: np.ndarray) -> tuple[float, float, float]:
    """（日频夏普，样本偏度，普通峰度）。T<3 时矩没有意义。"""
    n = len(r)
    if n < 3:
        raise ValueError(f"收益样本过短（{n}），置信度估计至少要 3 个观测")
    sd = float(r.std(ddof=1))
    if sd <= 1e-12:
        raise ValueError("收益零方差（横盘/常数序列），夏普无意义")
    sr = float(r.mean()) / sd
    from scipy.stats import kurtosis, skew

    g3 = float(skew(r, bias=True))
    g4 = float(kurtosis(r, fisher=False, bias=True))
    return sr, g3, g4


def _psr_stat(sr: float, g3: float, g4: float, t: int, sr_benchmark: float) -> float:
    denom = math.sqrt(max(1e-12, 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr))
    return normal_cdf((sr - sr_benchmark) * math.sqrt(t - 1) / denom)


def sharpe_ratio(returns, *, freq: int = 252) -> float:
    """年化夏普（ddof=1）。零方差 raise —— 报一个 inf 是在装。"""
    r = _as_returns(returns)
    sr, _, _ = _sr_moments(r)
    return sr * math.sqrt(freq)


def psr(returns, *, sr_benchmark: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio ∈ [0,1]。

    ``sr_benchmark`` 是**日频**基准夏普（年化基准请除以 sqrt(freq) 后传入）。
    返回「真实夏普 > 基准」的后验概率；0.5 = 与基准无差异，>0.95 才算老练。
    """
    r = _as_returns(returns)
    sr, g3, g4 = _sr_moments(r)
    return _psr_stat(sr, g3, g4, len(r), float(sr_benchmark))


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """N 个配置里「最好的那个」的期望噪声夏普（日频；Bailey & LdP 2014）。

    ``sr_var``：各试验**日频夏普**的样本方差（试验集合估计）。
    n_trials < 2 时没有多重检验可言，raise。
    """
    n = int(n_trials)
    if n < 2:
        raise ValueError(f"n_trials 必须 >= 2（单次试验没有选择偏差），收到 {n}")
    if not (sr_var > 0) or not math.isfinite(sr_var):
        raise ValueError(f"sr_var 必须为正的有限值，收到 {sr_var}")
    z1 = normal_ppf(1.0 - 1.0 / n)
    z2 = normal_ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(sr_var) * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def deflated_sharpe(returns, *, n_trials: int, sr_trials_var: float | None = None) -> dict:
    """Deflated Sharpe Ratio：把观测夏普相对 E[maxSR] 水位检验。

    ``sr_trials_var`` 缺省时用解析近似 Var[SR] ≈ (1 - γ3·SR + (γ4-1)/4·SR²)/(T-1)
    —— 这是**下界近似**（真实试验集的离散度通常更大），有条件就从试验台账
    里统计真实 var 传进来。
    返回 dict：dsr ∈ [0,1]、sharpe_daily、expected_max_sharpe_daily、n_trials。
    """
    r = _as_returns(returns)
    sr, g3, g4 = _sr_moments(r)
    t = len(r)
    if sr_trials_var is None:
        sr_trials_var = (1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr) / (t - 1)
    e_max = expected_max_sharpe(n_trials, float(sr_trials_var))
    dsr = _psr_stat(sr, g3, g4, t, e_max)
    return {
        "dsr": dsr,
        "sharpe_daily": sr,
        "expected_max_sharpe_daily": e_max,
        "n_trials": int(n_trials),
    }


def cscv_pbo(returns_matrix, *, n_partitions: int = 16) -> dict:
    """CSCV-PBO：组合对称交叉验证的过拟合概率（Bailey et al. 2017）。

    ``returns_matrix``：形状 (T, N)，T 期 × N 个配置的日收益（列 = 配置）。
    把 T 期按时间均分成 k 个偶数块（尾部不足一块的观测丢弃），枚举全部
    C(k, k/2) 种「一半块做样本内」的组合：样本内均值排名最优的配置，在
    样本外的排名是否跌到中位以下（λ ≤ 0，Bailey et al. 2017 口径）。

    返回 dict：pbo ∈ [0,1]（越低越好；纯噪声 ≈ 0.5，>0.5 说明排名在
    样本内外**系统性反转** —— 比碰巧还差）、n_combos、n_partitions、t_used。
    """
    m = np.asarray(returns_matrix, dtype=float)
    if m.ndim != 2:
        raise ValueError(f"returns_matrix 须为二维 (T, N)，收到 shape {m.shape}")
    t_total, n_cfg = m.shape
    if t_total < 4 or n_cfg < 2:
        raise ValueError(f"CSCV 至少要 4 期 × 2 配置，收到 {t_total}×{n_cfg}")
    if not np.all(np.isfinite(m)):
        raise ValueError("收益矩阵含 NaN/Inf，先清洗再算 PBO")

    k = min(int(n_partitions), t_total)
    if k % 2:
        k -= 1
    if k < 4:
        raise ValueError(f"样本太短（T={t_total}）：偶数块数至少 4，或调小 n_partitions")
    block = t_total // k
    t_used = k * block
    block_means = m[:t_used].reshape(k, block, n_cfg).mean(axis=1)  # (k, N)

    combos = np.array(list(combinations(range(k), k // 2)))  # (C, k/2)
    is_means = block_means[combos].mean(axis=1)  # (C, N)
    oos_means = (block_means.sum(axis=0) - is_means) / (k - k // 2)
    j_star = is_means.argmax(axis=1)  # 样本内冠军
    rows = np.arange(len(combos))
    # 相对排名 ω：1 = 冠军在样本外**最差**（Bailey et al. 2017 口径）——
    # ω 越小越像过拟合（样本内选出的冠军样本外垫底）
    omega = (oos_means < oos_means[rows, j_star][:, None]).sum(axis=1) + 1
    lam = np.log(omega / (n_cfg + 1 - omega))
    return {
        # λ ≤ 0 ⇔ 冠军的样本外排名掉到中位以下（含 N 奇数时的中位本身）
        "pbo": float((lam <= 0).mean()),
        "n_combos": int(len(combos)),
        "n_partitions": k,
        "t_used": int(t_used),
    }
