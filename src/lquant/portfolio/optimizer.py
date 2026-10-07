"""基准相对组合优化（借 qlib ``EnhancedIndexingOptimizer`` 的语义）。

目标：**在跟踪误差约束下最大化预期超额**。与 ``weighting.py`` 里那些
「只优化权重形状」的方法不同，这里有一个明确的对手 —— 基准组合 ``b``：

    max_w  αᵀ(w - b)          # 预期超额
    s.t.   (w - b)ᵀ Σ (w - b) ≤ TE²   # 跟踪误差上限
           Σw = 1, 0 ≤ w ≤ max_weight
           |w_i - b_i| ≤ max_active  # 单只主动权重上限

为什么需要它：纯 α 最大化会给出极度集中的组合（押注估计误差），而纯
最小方差会把 α 全丢掉。TE 约束是这两者之间的**显式旋钮** ——
「我愿意偏离基准多少」是投资决策，不该藏在优化器的隐含正则里。

## 三个必须显式处理的点

1. **协方差口径**：TE 约束用 ``portfolio.riskmodel`` 的估计（T<N 时样本
   协方差奇异，TE 会被低估到 0，约束形同虚设）。
2. **权重和与边界**：优化器不保证 Σw=1 的数值精度，出来后统一 ``_clean``。
3. **不可行情形**：约束太紧（TE 太小 + 主动上限太小）时 SLSQP 会失败 ——
   此时**退回基准权重**（主动权重全 0），而不是返回一个违反约束的解。
   这比"报错让上游崩"或"返回越界解"都安全：基准组合至少是可解释的。
"""
from __future__ import annotations

import numpy as np

from lquant.core.errors import LQuantError

__all__ = ["EnhancedIndexingResult", "tracking_error", "active_share",
           "enhanced_indexing_weight", "OptimizerError"]


class OptimizerError(LQuantError):
    """组合优化不可行/参数非法。"""


def _as_weights(benchmark, symbols: list[str]) -> np.ndarray:
    """基准权重 → 与 symbols 对齐的数组（缺失补 0，超集丢弃）。"""
    if isinstance(benchmark, dict):
        b = np.array([float(benchmark.get(s, 0.0)) for s in symbols])
    else:
        b = np.asarray(benchmark, dtype=float).ravel()
        if len(b) != len(symbols):
            raise OptimizerError(
                f"基准权重长度 {len(b)} 与标的数 {len(symbols)} 不一致")
    b = np.nan_to_num(b, nan=0.0, posinf=0.0, neginf=0.0)
    b = np.clip(b, 0.0, None)
    tot = b.sum()
    if tot <= 1e-12:
        raise OptimizerError("基准权重全为 0（无法定义主动权重）")
    return b / tot


def tracking_error(w: np.ndarray, b: np.ndarray, cov: np.ndarray) -> float:
    """主动权重的年化跟踪误差（日频协方差 → ``×sqrt(252)``）。

    与 ``backtest/attribution.py::risk_vs_benchmark`` 的口径一致：TE 是
    **主动收益的标准差**，用协方差形式表达即 ``sqrt(dᵀΣd)``。
    """
    d = np.asarray(w, dtype=float) - np.asarray(b, dtype=float)
    var = float(d @ cov @ d)
    return float(np.sqrt(max(var, 0.0)) * np.sqrt(252))


def active_share(w: np.ndarray, b: np.ndarray) -> float:
    """主动份额 = ``0.5 · Σ|w_i - b_i|``（0 = 完全复制基准）。"""
    return float(0.5 * np.abs(np.asarray(w) - np.asarray(b)).sum())


class EnhancedIndexingResult(dict):
    """优化结果：权重 + 诊断。行为与 ``dict`` 一致（可直接当权重用）。"""

    @property
    def diagnostics(self) -> dict:
        return {k: v for k, v in self.items() if k.startswith("_")}

    def weights(self) -> dict[str, float]:
        return {k: v for k, v in self.items() if not k.startswith("_")}


def enhanced_indexing_weight(
    returns,
    symbols: list[str] | None = None,
    *,
    benchmark_weights=None,
    scores: dict[str, float] | None = None,
    expected_returns=None,
    te_target: float = 0.05,
    max_weight: float = 0.10,
    max_active: float | None = None,
    cov_method: str | None = "shrink_lw",
    cov_params: dict | None = None,
    risk_aversion: float = 0.0,
    max_turnover: float | None = None,
    prev_weights=None,
) -> EnhancedIndexingResult:
    """在跟踪误差约束下最大化预期超额。

    Parameters
    ----------
    benchmark_weights : dict ``{symbol: weight}`` 或与 ``symbols`` 等长的数组。
        **None（默认）= 等权基准**，不是「省略」。等权是 A 股最常用的大盘代理
        （与 ``backtest/benchmark.py::equal_weight_benchmark`` 同一口径），
        且这条默认值让本方法能被通用入口 ``weighting.weights(method="enhanced_indexing")``
        调到 —— 注册表里的方法必须都能被统一入口调用，否则就退化成
        AlphaPurify 那种「列得出、调不到」。用了默认值时 ``_benchmark_source``
        会标成 ``equal_weight_default``，别把它当成真指数基准。
    scores : ``{symbol: 分数}``；作为预期收益的**截面标准化**代理
        （分数本身不是收益率，直接当 α 用会让目标函数量纲失真）。
    expected_returns : 直接给预期收益（与 symbols 等长或 dict）；给了就忽略 scores。
    te_target : 年化跟踪误差上限（0.05 = 5%）。
    max_weight : 单只权重上限。
    max_active : 单只主动权重上限（None = 不额外限制，只受 ``max_weight`` 约束）。
    cov_method : 协方差口径，默认 ``shrink_lw`` —— TE 约束对协方差估计极敏感，
        样本口径在 T<N 时会把 TE 低估到 0，约束失去意义。
    risk_aversion : 可选的风险厌恶项（``αᵀd - λ·dᵀΣd``）；0 = 纯约束优化。
    max_turnover : 本次调仓的换手上限 —— ``Σ|w - w_prev| ≤ max_turnover``（L1）。
        cost_matrix 已经证明高换手是收益杀手，这里给权重层装刹车。
    prev_weights : 上期权重（dict 或数组）；``max_turnover`` 给了就必须给
        （没有上期权重就没有「换手」可言）。缺省 None = 以基准为原点，
        不启用换手约束。

    返回 :class:`EnhancedIndexingResult`（``dict`` 子类，带 ``_`` 前缀的诊断字段）。
    """
    from lquant.portfolio.weighting import _returns_matrix

    M, syms = _returns_matrix(returns, symbols)
    n = len(syms)
    if n < 1:
        raise OptimizerError("没有标的")
    if te_target <= 0:
        raise OptimizerError(f"te_target 必须为正，收到 {te_target}")

    if max_turnover is not None:
        if float(max_turnover) <= 0:
            raise OptimizerError(f"max_turnover 必须为正，收到 {max_turnover}")
        if prev_weights is None:
            raise OptimizerError(
                "max_turnover 必须配 prev_weights：没有上期权重就没有「换手」可言")

    if benchmark_weights is None:
        b = np.full(n, 1.0 / n)
        benchmark_source = "equal_weight_default"
    else:
        b = _as_weights(benchmark_weights, syms)
        benchmark_source = "provided"

    # 换手的参照点是上期持仓（不是基准）：上期怎么配的，决定这次要动多少
    w0 = _as_weights(prev_weights, syms) if prev_weights is not None else b

    # 预期收益：直接给 → 用；给分数 → 截面标准化（分数不是收益率）
    if expected_returns is not None:
        if isinstance(expected_returns, dict):
            alpha = np.array([float(expected_returns.get(s, 0.0)) for s in syms])
        else:
            alpha = np.asarray(expected_returns, dtype=float).ravel()
            if len(alpha) != n:
                raise OptimizerError("expected_returns 长度与标的数不一致")
    elif scores is not None:
        raw = np.array([float(scores.get(s, 0.0)) for s in syms])
        sd = float(np.std(raw, ddof=1)) if n > 1 else 0.0
        alpha = (raw - raw.mean()) / sd if sd > 1e-12 else np.zeros(n)
        # 标定到日频收益量级：1 个标准差 ≈ 10bp/日，避免 α 与 Σ 量纲失配
        alpha = alpha * 0.001
    else:
        raise OptimizerError("必须给 scores 或 expected_returns（否则目标函数无意义）")
    alpha = np.nan_to_num(alpha, nan=0.0, posinf=0.0, neginf=0.0)

    from lquant.portfolio.weighting import _cov_for

    cov = _cov_for(M, cov_method, cov_params)

    lo = np.zeros(n)
    hi = np.full(n, float(max_weight))
    if max_active is not None:
        if max_active <= 0:
            raise OptimizerError(f"max_active 必须为正，收到 {max_active}")
        lo = np.maximum(lo, b - float(max_active))
        hi = np.minimum(hi, b + float(max_active))
    if lo.sum() > 1.0 + 1e-9 or hi.sum() < 1.0 - 1e-9:
        # 边界与「权重和为 1」矛盾 → 无法行。直接退回基准（见模块 docstring）
        return _fallback(b, syms, cov, "权重边界与 Σw=1 矛盾", benchmark_source,
                         w0=w0, max_turnover=max_turnover, alpha=alpha, cov_method=cov_method)

    te_daily = float(te_target) / np.sqrt(252)

    def objective(w: np.ndarray) -> float:
        d = w - b
        val = -float(alpha @ d)                     # 最大化超额 = 最小化负超额
        if risk_aversion > 0:
            val += float(risk_aversion) * float(d @ cov @ d)
        return val

    def te_constraint(w: np.ndarray) -> float:
        d = w - b
        return te_daily ** 2 - float(d @ cov @ d)   # ≥ 0 表示满足约束

    def turnover_constraint(w: np.ndarray) -> float:
        # |·| 在 w=w0 处不可微，SLSQP 会迭代到上限不收敛 —— 平滑 L1：
        # sqrt(x²+ε) ≥ |x|（约束略偏紧），核验仍用真 L1，方向安全
        diff = w - w0
        smooth = float(np.sqrt(diff * diff + 1e-10).sum())
        return float(max_turnover) - smooth

    constraints = [
        {"type": "eq", "fun": lambda w: float(w.sum() - 1.0)},
        {"type": "ineq", "fun": te_constraint},
    ]
    if max_turnover is not None:
        constraints.append({"type": "ineq", "fun": turnover_constraint})

    try:
        from scipy.optimize import minimize

        # 换手约束下从上期权重出发（可行点）；否则从基准出发
        anchor = w0 if max_turnover is not None else b
        x0 = np.clip(anchor, lo, hi)
        x0 = x0 / x0.sum() if x0.sum() > 1e-12 else anchor
        res = minimize(
            objective, x0, method="SLSQP",
            bounds=list(zip(lo, hi, strict=True)),
            constraints=constraints,
            options={"maxiter": 500, "ftol": 1e-12},
        )
    except ImportError as e:  # pragma: no cover - scipy 是既有依赖
        raise OptimizerError("基准相对优化需要 scipy") from e

    if not np.all(np.isfinite(res.x)):
        return _fallback(b, syms, cov, "优化解含非有限值", benchmark_source,
                         w0=w0, max_turnover=max_turnover, alpha=alpha, cov_method=cov_method)
    # res.success 不做一票否决：小量纲 α 下 SLSQP 常迭代到上限才停，但解
    # 往往已可行 —— 可行性由下面的 TE/换手核验说了算，不冤枉可行解也不
    # 放行越界解（未收敛而核验又不过的，照旧 fallback 并带原因）

    w = np.clip(np.asarray(res.x, dtype=float), lo, hi)
    tot = w.sum()
    if tot <= 1e-12:
        return _fallback(b, syms, cov, "解全为 0", benchmark_source,
                         w0=w0, max_turnover=max_turnover, alpha=alpha, cov_method=cov_method)
    w = w / tot

    # 收敛后仍可能因数值误差轻微越界 → 显式核验，越界就退回基准
    realized_te = tracking_error(w, b, cov)
    if realized_te > float(te_target) * 1.001:
        return _fallback(b, syms, cov,
                         f"解违反 TE 约束（{realized_te:.4f} > {te_target:.4f}）",
                         benchmark_source, w0=w0, max_turnover=max_turnover,
                         alpha=alpha, cov_method=cov_method)

    # 换手约束同样事后核验 —— SLSQP 的 ineq 在非光滑 |·| 上可能轻微越界
    realized_turnover = float(np.abs(w - w0).sum())
    if max_turnover is not None and realized_turnover > float(max_turnover) * 1.001:
        return _fallback(b, syms, cov,
                         f"解违反换手约束（{realized_turnover:.4f} > "
                         f"{float(max_turnover):.4f}）", benchmark_source,
                         w0=w0, max_turnover=max_turnover, alpha=alpha, cov_method=cov_method)

    out = EnhancedIndexingResult({s: float(v) for s, v in zip(syms, w, strict=True)})
    out["_tracking_error"] = realized_te
    out["_turnover_from_prev"] = realized_turnover
    out["_active_share"] = active_share(w, b)
    out["_expected_excess"] = float(alpha @ (w - b))
    out["_cov_method"] = cov_method or "sample"
    out["_benchmark_source"] = benchmark_source
    out["_fallback"] = False
    out["_fallback_reason"] = None
    out["_n_active"] = int((np.abs(w - b) > 1e-6).sum())
    return out


def _fallback(b: np.ndarray, syms: list[str], cov: np.ndarray, reason: str,
              benchmark_source: str = "provided", *,
              w0: np.ndarray | None = None,
              max_turnover: float | None = None,
              alpha: np.ndarray | None = None,
              cov_method: str | None = None) -> EnhancedIndexingResult:
    """不可行时退回一个**已知可行**的权重组合。

    比「报错让上游崩」安全：一个违反约束的解会静默产生超出风险预算的暴露；
    退回上期持仓（换手 0）或基准（主动权重 0）都是可解释、可交易的。
    原因记在 ``_fallback_reason``。

    退给谁由约束决定：

    - 给了 ``max_turnover``（本次调仓有换手上限）：退回**上期持仓 w0** ——
      退回基准 b 的换手是 ``Σ|b - w0|``，可能远超声明的上限，于是「刹车」
      恰恰在最该生效的时候缺席。w0 的换手恒为 0，永远满足上限；
      TE / 主动份额 / 预期超额照实按 w0 vs b 计算，不谎报 0。
    - 没给换手上限：维持原语义退回基准（主动权重全 0，TE = 0）。
    """
    no_trade = max_turnover is not None and w0 is not None
    w = w0 if no_trade else b
    out = EnhancedIndexingResult({s: float(v) for s, v in zip(syms, w, strict=True)})
    if no_trade:
        reason = f"{reason} → 保持上期持仓（换手上限 {float(max_turnover)} 下无可行解）"
        out["_tracking_error"] = float(tracking_error(w, b, cov))
        out["_turnover_from_prev"] = 0.0
        out["_active_share"] = float(active_share(w, b))
        out["_expected_excess"] = float(alpha @ (w - b)) if alpha is not None else 0.0
        out["_cov_method"] = cov_method or "sample"
        out["_n_active"] = int((np.abs(w - b) > 1e-6).sum())
    else:
        out["_tracking_error"] = 0.0
        out["_turnover_from_prev"] = None
        out["_active_share"] = 0.0
        out["_expected_excess"] = 0.0
        out["_cov_method"] = "n/a"
        out["_n_active"] = 0
    out["_benchmark_source"] = benchmark_source
    out["_fallback"] = True
    out["_fallback_reason"] = reason
    return out
