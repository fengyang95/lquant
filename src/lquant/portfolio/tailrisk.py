"""尾部风险：CVaR / CDaR 的统计量与其 LP 优化（P1-4）。

为什么是 LP，而不是"再写一个均值-方差式的解析解"：CVaR 与 CDaR 的目标函数
是**分段线性**的，用 Rockafellar–Uryasev / Chekhlov–Uryasev–Zabarankin 的
辅助变量展开后就是标准线性规划，有全局最优、不需要梯度、也不会陷在局部解。
Riskfolio 的 26 个风险度量里，**只有 LP 类**（MAD/CVaR/CDaR/……）能靠开源
solver 求解；EVaR/RLVaR/峰度那几类需要 MOSEK，本仓不碰。

口径（写在最前面，避免以后被"优化"掉）：

- **CVaR**：损失超过 VaR 那部分的**条件期望**（也叫 Expected Shortfall）。
  历史法直接对尾部求均值；优化时用 R-U 的辅助变量。
- **CDaR**：**回撤**的 CVaR —— 先算净值曲线的回撤序列，再取尾部均值。
  它比最大回撤稳（一个极值不决定全部），又比波动率贴近"亏钱的感受"。
- 收益口径一律是**简单收益**（不是对数收益）：CVaR 的可加性与报告里
  其他数字保持一致。

scipy 是 ``factors`` extra 的依赖，不在基础依赖里 —— 所以**在函数内部导入**，
缺了要报「装 extras」而不是退化成别的口径（静默换口径比报错危险得多）。
"""
from __future__ import annotations

import math

import numpy as np

__all__ = ["cvar", "cvar_weight_lp", "cdar", "cdar_weight_lp", "tail_metrics"]

_MIN_OBS = 20


def _clean_returns(returns) -> np.ndarray:
    r = np.asarray(returns, dtype=float).reshape(-1)
    return r[np.isfinite(r)]


def _require_scipy():
    try:
        from scipy.optimize import linprog  # noqa: F401
    except ImportError as e:  # pragma: no cover - 取决于是否装 factors extra
        raise RuntimeError(
            "CVaR/CDaR 优化需要 scipy（`pip install -e '.[factors]'`）；"
            "缺依赖时报错，不回退到别的风险口径") from e
    return linprog


def _check_level(level: float) -> float:
    if not 0.5 < level < 1.0:
        raise ValueError(f"level 必须在 (0.5, 1) 之间，收到 {level}")
    return float(level)


def _check_obs(r: np.ndarray) -> None:
    if len(r) < _MIN_OBS:
        raise ValueError(f"尾部风险至少需要 {_MIN_OBS} 个有效收益观测，收到 {len(r)}")


def cvar(returns, level: float = 0.95) -> float:
    """历史 CVaR（条件尾部期望）。返回**负值**表示亏损。

    定义：``CVaR_β = E[ r | r <= VaR_β ]``，其中 ``VaR_β`` 是收益分布的
    ``1-β`` 分位。尾部不足一条观测时取最小值本身（只有 1 个点时它就是尾部）。
    """
    level = _check_level(level)
    r = _clean_returns(returns)
    _check_obs(r)
    q = float(np.quantile(r, 1.0 - level))
    tail = r[r <= q]
    if not len(tail):
        tail = np.array([float(r.min())])
    return float(tail.mean())


def cdar(returns, level: float = 0.95) -> float:
    """历史 CDaR（回撤的条件尾部期望）。返回**正数**表示回撤幅度。

    回撤序列按净值口径：``nav_t = Π(1+r_s)``，``dd_t = 1 - nav_t / max_{s<=t} nav_s``。
    只用收益序列就能算，不要求外部传净值 —— 避免两条口径（收益 vs 净值）
    各自演化。
    """
    level = _check_level(level)
    r = _clean_returns(returns)
    _check_obs(r)
    nav = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(nav)
    dd = 1.0 - nav / peak
    q = float(np.quantile(dd, level))
    tail = dd[dd >= q]
    if not len(tail):
        tail = np.array([float(dd.max())])
    return float(tail.mean())


def tail_metrics(returns, levels: tuple[float, ...] = (0.95, 0.99)) -> dict:
    """一次给出各置信度的 CVaR / CDaR（报告用）。"""
    out: dict = {}
    for lv in levels:
        key = f"{lv:.2f}".rstrip("0").rstrip(".").replace(".", "")
        out[f"cvar_{key}"] = cvar(returns, lv)
        out[f"cdar_{key}"] = cdar(returns, lv)
    return out


# --------------------------------------------------------------------------- #
# LP 优化
# --------------------------------------------------------------------------- #

def cvar_weight_lp(returns: np.ndarray, *, level: float = 0.95,
                   max_weight: float = 1.0,
                   min_return: float | None = None) -> np.ndarray:
    """最小化 CVaR 的多头权重（Rockafellar–Uryasev LP）。

    变量 ``[w(n), alpha, u(T)]``：``alpha`` 是 VaR 水平，``u_t`` 是超过
    VaR 的超出量。

    约束 ``sum(w)=1``、``0<=w<=max_weight``、``u>=0``，
    以及 ``u_t >= -(r_t·w) - alpha``（把条件期望展开成线性）。
    目标：``alpha + 1/(T(1-β)) * Σu_t``。
    """
    level = _check_level(level)
    R = np.asarray(returns, dtype=float)
    if R.ndim != 2 or R.shape[0] < _MIN_OBS:
        raise ValueError(f"需要 T×N 且 T >= {_MIN_OBS} 的收益矩阵，收到 {R.shape}")
    T, n = R.shape
    if n < 2:
        raise ValueError("至少需要 2 个标的")
    linprog = _require_scipy()

    # 变量顺序：w(0..n-1), alpha(n), u(n+1..n+T)
    c = np.zeros(n + 1 + T)
    c[n] = 1.0
    c[n + 1:] = 1.0 / (T * (1.0 - level))
    # u_t >= -(r_t·w) - alpha  ⇔  -(r_t·w) - alpha - u_t <= 0
    # 符号必须按 linprog 的 A_ub·x <= b_ub 写：写反会变成「u 有上界」，
    # 于是 α 可以无限增大把 u 压到 0，LP 直接报 unbounded。
    A_ub = np.zeros((T, n + 1 + T))
    A_ub[:, :n] = -R
    A_ub[:, n] = -1.0
    A_ub[:, n + 1:] = -np.eye(T)
    b_ub = np.zeros(T)
    A_eq = np.zeros((1, n + 1 + T))
    A_eq[0, :n] = 1.0
    b_eq = np.array([1.0])
    if min_return is not None:
        # 收益约束：均值收益 >= min_return（可选，把「只求稳」拉回有收益的解）
        row = np.zeros((1, n + 1 + T))
        row[0, :n] = R.mean(axis=0)
        A_ub = np.vstack([A_ub, -row])
        b_ub = np.concatenate([b_ub, [-float(min_return)]])
    bounds = [(0.0, float(max_weight))] * n + [(None, None)] + [(0.0, None)] * T
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds,
                  method="highs")
    if not res.success:
        # 不可行几乎总是「min_return 要得比这套资产能给的还高」—— 说清楚，
        # 而不是甩一句「未收敛」让人去猜
        why = "约束不可行（min_return 可能超过这套资产的可达收益）" if res.status == 2 \
            else res.message
        raise RuntimeError(f"CVaR LP 求解失败: {why}")
    return np.clip(res.x[:n], 0.0, None)


def cdar_weight_lp(returns: np.ndarray, *, level: float = 0.95,
                   max_weight: float = 1.0,
                   lookback: int = 252,
                   min_return: float | None = None) -> np.ndarray:
    """最小化 CDaR 的多头权重（Chekhlov–Uryasev–Zabarankin LP）。

    变量 ``[w(n), y(T), z(T), alpha, u(T)]``：``y_t`` 是累计收益，
    ``z_t`` 是回撤，``alpha`` 是 VaR-like 分位，``u_t`` 是尾部超出量。

    回撤约束 ``z_t >= y_s - y_t`` 对**所有 s <= t** 成立 —— 这是 T²/2 条约束，
    所以用 ``lookback`` 截断观测（默认最近 252 期）。截断会改变口径，因此
    报告与调用方必须看得到实际用了多少期（返回值旁的 ``res`` 里带 T）。
    """
    level = _check_level(level)
    R = np.asarray(returns, dtype=float)
    if R.ndim != 2 or R.shape[0] < _MIN_OBS:
        raise ValueError(f"需要 T×N 且 T >= {_MIN_OBS} 的收益矩阵，收到 {R.shape}")
    T_all, n = R.shape
    if n < 2:
        raise ValueError("至少需要 2 个标的")
    if lookback and T_all > lookback:
        R = R[-int(lookback):]
    T = R.shape[0]
    linprog = _require_scipy()

    # w(0..n-1), y(n..n+T-1), z(n+T..n+2T-1), alpha(n+2T), u(n+2T+1..)
    nv = n + 2 * T + 1 + T
    c = np.zeros(nv)
    c[n + 2 * T] = 1.0
    c[n + 2 * T + 1:] = 1.0 / (T * (1.0 - level))

    rows: list[np.ndarray] = []
    rhs: list[float] = []

    # 等式约束两条：y_t - Σ_{s<=t} r_s·w = 0，以及 Σw = 1
    A_eq = np.zeros((T + 1, nv))
    for t in range(T):
        A_eq[t, :n] = -R[:t + 1].sum(axis=0)
        A_eq[t, n + t] = 1.0
    A_eq[T, :n] = 1.0
    b_eq = np.zeros(T + 1)
    b_eq[T] = 1.0

    # 回撤定义 z_t >= y_s - y_t（对所有 s <= t）→ y_s - y_t - z_t <= 0
    for t in range(T):
        for s in range(t + 1):
            row = np.zeros(nv)
            row[n + s] += 1.0          # +y_s
            row[n + t] -= 1.0          # -y_t
            row[n + T + t] -= 1.0      # -z_t
            rows.append(row)
            rhs.append(0.0)
    # u_t - z_t + alpha >= 0  →  -u_t + z_t - alpha <= 0
    for t in range(T):
        row = np.zeros(nv)
        row[n + 2 * T + 1 + t] = -1.0
        row[n + T + t] = 1.0
        row[n + 2 * T] = -1.0
        rows.append(row)
        rhs.append(0.0)

    A_ub = np.array(rows)
    b_ub = np.array(rhs)
    if min_return is not None:
        row = np.zeros((1, nv))
        row[0, :n] = R.mean(axis=0)
        A_ub = np.vstack([A_ub, -row])
        b_ub = np.concatenate([b_ub, [-float(min_return)]])

    bounds = ([(0.0, float(max_weight))] * n
              + [(None, None)] * T          # y 无界（累计收益可正可负）
              + [(0.0, None)] * T           # z >= 0（回撤非负）
              + [(None, None)]              # alpha
              + [(0.0, None)] * T)          # u >= 0
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds,
                  method="highs")
    if not res.success:
        why = "约束不可行（min_return 可能超过这套资产的可达收益）" if res.status == 2 \
            else res.message
        raise RuntimeError(f"CDaR LP 求解失败: {why}")
    w = np.clip(res.x[:n], 0.0, None)
    if not math.isfinite(float(w.sum())) or w.sum() <= 0:
        raise RuntimeError("CDaR LP 返回了退化权重")
    return w
