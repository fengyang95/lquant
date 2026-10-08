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
3. **不可行情形**：约束太紧（TE 太小 + 主动上限太小 + 换手上限）时 SLSQP
   会失败 —— 此时退回一个**显式、可解释**的组合，并如实报告它相对已声明
   约束的违约情况（``_constraint_violations``）。有换手上限且失败发生在
   求解环节时优先保持上期持仓（换手最小），否则退回基准（主动权重全 0）。
   这比"报错让上游崩"或"返回越界解"安全，但**不谎称退回的一定可行** ——
   例如上期持仓本身可能已经超出本次声明的 TE/主动上限，诊断里必须看得见。
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


def _as_prev_positions(prev, symbols: list[str]) -> tuple[np.ndarray, float, float]:
    """上期持仓 → 与 symbols 对齐的**原始**权重 + 池外腿 + 上期总仓位。

    与 ``_as_weights``（基准，归一化到 Σ=1）刻意不同：换手约束的参照点必须
    保留上期原始尺度。``apply_no_trade_band`` 会产生 Σ<1 的「权重 + 现金」
    组合，上期也可能持有本期符号表之外的票；一旦在这里归一化/裁剪，回补
    现金腿的买入和清池外票的卖出就完全不进 ``Σ|w - w_prev|``，声明的换手
    上限在真实成交 8~20 倍的情况下依然"成立"。

    返回 ``(p, off_assets, total_prev)``：

    - ``p``：``symbols`` 的上期原始权重（dict 缺键补 0；数组须等长）。
    - ``off_assets``：上期持有但**不在本期符号表**里的权重合计（必须清掉的腿）。
    - ``total_prev``：上期全部资产权重合计（``Σp + off_assets``）；它与 1 的
      差额是上期现金。三者一起决定真实成交 L1（见 :func:`_true_turnover`）。

    负数/非有限权重是数据错误，``total_prev > 1`` 说明上期含杠杆、无法构建
    现金腿 —— 两者都显式 raise，不静默裁剪。
    """
    if isinstance(prev, dict):
        p = np.array([float(prev.get(s, 0.0)) for s in symbols])
        sym_set = set(symbols)
        off_assets = float(sum(float(v) for s, v in prev.items() if s not in sym_set))
    else:
        p = np.asarray(prev, dtype=float).ravel()
        if len(p) != len(symbols):
            raise OptimizerError(
                f"上期权重长度 {len(p)} 与标的数 {len(symbols)} 不一致")
        off_assets = 0.0
    total_prev = float(p.sum()) + off_assets
    if not np.all(np.isfinite(p)) or not np.isfinite(total_prev):
        raise OptimizerError("上期权重含非有限值（NaN/inf）：换手参照点无法定义")
    if (p < -1e-12).any() or off_assets < -1e-12:
        raise OptimizerError("上期权重含负值：本平台不做空，无法定义换手")
    if total_prev > 1.0 + 1e-9:
        raise OptimizerError(
            f"上期权重合计 {total_prev:.6f} > 1（含杠杆）：无法构建现金腿，"
            "换手参照点不成立")
    return p, float(max(0.0, off_assets)), float(max(0.0, total_prev))


def _true_turnover(w: np.ndarray, prev: np.ndarray,
                   off_assets: float, total_prev: float) -> float:
    """真实成交 L1：``Σ|w - w_prev| + |Δ现金| + 清池外腿``。

    ``prev`` 是原始上期持仓，``off_assets`` 是池外持仓合计，``total_prev``
    是上期总仓位（现金 = ``1 - total_prev``）。展开成交明细（输出 w 未必
    满仓，Σw ≤ 1）：

        Σ|w - p|                    # 场内调仓
      + off_assets                  # 清掉池外持仓
      + |(1 - Σw) - (1 - total_prev)|   # 现金腿变化
      = Σ|w - p| + off_assets + |total_prev - Σw|

    **必须**用这个口径：原先 ``Σ|w - 归一化(prev)|`` 会把「补现金腿 / 清池外
    票」的真实成交全部抹掉，声明的换手上限在真实成交 8~20 倍时依然"成立"。
    """
    w = np.asarray(w, dtype=float)
    return float(np.abs(w - prev).sum() + off_assets + abs(total_prev - float(w.sum())))


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
    max_turnover : 本次调仓的换手上限 —— ``Σ|w - w_prev| + |Δ现金| ≤ max_turnover``
        （L1，含现金腿；满仓输出时即 ``Σ|w - w_prev| + (1 - Σw_prev)``）。
        cost_matrix 已经证明高换手是收益杀手，这里给权重层装刹车。
    prev_weights : 上期**原始**持仓（dict 或数组）；``max_turnover`` 给了就必须给
        （没有上期权重就没有「换手」可言）。缺省 None = 以基准为原点，
        不启用换手约束。口径**不归一化**：Σ<1 的差额（现金缓冲）与不在
        ``symbols`` 里的持仓都计入真实成交 L1 —— 它们本期必须被买回/清掉；
        Σ>1（杠杆）无法构建现金腿，显式 raise。

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

    # 换手的参照点是上期持仓（不是基准）：上期怎么配的，决定这次要动多少。
    # 保留**原始**尺度，不归一化 —— 归一化会把补现金腿/清池外票的真实成交
    # 从参照点里抹掉，声明的上限随之失效（见 _as_prev_positions）。
    if prev_weights is not None:
        w0, off_assets, total_prev = _as_prev_positions(prev_weights, syms)
    else:
        w0, off_assets, total_prev = b, 0.0, 1.0
    # 满仓输出（Σw=1）下的常数换手腿 = 上期没落在本期符号表里的部分
    # （池外持仓 + 现金）= 1 - Σw0；SLSQP 的约束用它，核验用通用式。
    off_share = 1.0 - float(w0.sum())

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

    def _fb(reason: str, *, turnover_caused: bool) -> EnhancedIndexingResult:
        """退回显式候选（见 :func:`_fallback`）。

        ``turnover_caused`` 决定是否优先退上期持仓：结构性边界矛盾与换手
        刹车无关，退回基准更诚实（上期持仓未必满足那些边界）。
        """
        return _fallback(b, syms, cov, reason, benchmark_source,
                         w0=w0, max_turnover=max_turnover, alpha=alpha,
                         cov_method=cov_method, te_target=float(te_target),
                         max_weight=float(max_weight), max_active=max_active,
                         off_assets=off_assets, total_prev=total_prev,
                         turnover_caused=turnover_caused)

    # 逐元素预检：b_i > max_weight + max_active 时 lo_i > hi_i —— scipy 的
    # _validate_bounds 会抛裸 ValueError（不是 LQuantError，调用方的
    # except LQuantError 抓不住，也完全绕过 fallback 契约）。和式预检
    # （Σhi ≥ 1 ≥ Σlo）对这种情形恒放行，必须在调用前就拦下。
    if bool((lo > hi + 1e-12).any()):
        bad = int(np.argmax(lo - hi))
        return _fb(
            f"单只边界自相矛盾：{syms[bad]} 下界 {lo[bad]:.4f} > 上界 "
            f"{hi[bad]:.4f}（基准权重 {b[bad]:.4f} 超出 "
            f"max_weight + max_active）", turnover_caused=False)
    if lo.sum() > 1.0 + 1e-9 or hi.sum() < 1.0 - 1e-9:
        # 边界与「权重和为 1」矛盾 → 无法行（与换手约束无关，不优先退上期）
        return _fb("权重边界与 Σw=1 矛盾", turnover_caused=False)

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
        # sqrt(x²+ε) ≥ |x|（约束略偏紧），核验仍用真 L1，方向安全。
        # off_share 是常数腿（上期池外持仓 + 现金），直接占预算。
        diff = w - w0
        smooth = float(np.sqrt(diff * diff + 1e-10).sum()) + off_share
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
    except ValueError as e:
        # 兜底：scipy 校验类错误（边界/约束格式）也走 fallback，绝不裸抛
        return _fb(f"优化器拒绝该问题：{e}", turnover_caused=True)

    if not np.all(np.isfinite(res.x)):
        return _fb("优化解含非有限值", turnover_caused=True)
    # res.success 不做一票否决：小量纲 α 下 SLSQP 常迭代到上限才停，但解
    # 往往已可行 —— 可行性由下面的 TE/换手核验说了算，不冤枉可行解也不
    # 放行越界解（未收敛而核验又不过的，照旧 fallback 并带原因）

    w = np.clip(np.asarray(res.x, dtype=float), lo, hi)
    tot = w.sum()
    if tot <= 1e-12:
        return _fb("解全为 0", turnover_caused=True)
    w = w / tot

    # 收敛后仍可能因数值误差轻微越界 → 显式核验，越界就退回
    realized_te = tracking_error(w, b, cov)
    if realized_te > float(te_target) * 1.001:
        return _fb(f"解违反 TE 约束（{realized_te:.4f} > {te_target:.4f}）",
                   turnover_caused=True)

    # 换手约束同样事后核验 —— SLSQP 的 ineq 在非光滑 |·| 上可能轻微越界
    realized_turnover = _true_turnover(w, w0, off_assets, total_prev)
    if max_turnover is not None and realized_turnover > float(max_turnover) * 1.001:
        return _fb(f"解违反换手约束（{realized_turnover:.4f} > "
                   f"{float(max_turnover):.4f}）", turnover_caused=True)

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
    # 正常解也应可核验：诊断字段与 fallback 路径同口径（通常为空）
    out["_constraint_violations"] = _constraint_violations(
        w, b, cov, te_target=float(te_target), max_weight=float(max_weight),
        max_active=max_active, max_turnover=max_turnover, prev=w0,
        off_assets=off_assets, total_prev=total_prev)
    return out


def _constraint_violations(w: np.ndarray, b: np.ndarray, cov: np.ndarray, *,
                           te_target: float, max_weight: float | None,
                           max_active: float | None, max_turnover: float | None,
                           prev: np.ndarray | None,
                           off_assets: float = 0.0,
                           total_prev: float = 1.0) -> list[str]:
    """逐个核验候选解相对**已声明**约束的违约项（空列表 = 全部满足）。

    fallback 也必须说真话：退回上期持仓可能本身就超出本次声明的 TE /
    主动上限 / 单票上限（上期组合不是为本次约束准备的）。这些违约必须
    出现在诊断里，而不是被「已知可行」的说法吞掉。
    """
    v: list[str] = []
    w = np.asarray(w, dtype=float)
    s = float(w.sum())
    if s > 1.0 + 1e-6:
        v.append(f"权重和 {s:.4f} > 1（隐含杠杆）")
    if max_weight is not None and float(w.max()) > float(max_weight) + 1e-9:
        v.append(f"max_weight：最大权重 {float(w.max()):.4f} > {float(max_weight):.4f}")
    if max_active is not None:
        act = float(np.abs(w - np.asarray(b, dtype=float)).max())
        if act > float(max_active) + 1e-9:
            v.append(f"max_active：最大主动权重 {act:.4f} > {float(max_active):.4f}")
    te = tracking_error(w, b, cov)
    if te > float(te_target) * 1.001:
        v.append(f"tracking_error {te:.4f} > te_target {float(te_target):.4f}")
    if max_turnover is not None and prev is not None:
        tu = _true_turnover(w, prev, off_assets, total_prev)
        if tu > float(max_turnover) * 1.001:
            v.append(f"turnover {tu:.4f} > max_turnover {float(max_turnover):.4f}")
    return v


def _fallback(b: np.ndarray, syms: list[str], cov: np.ndarray, reason: str,
              benchmark_source: str = "provided", *,
              w0: np.ndarray | None = None,
              max_turnover: float | None = None,
              alpha: np.ndarray | None = None,
              cov_method: str | None = None,
              te_target: float | None = None,
              max_weight: float | None = None,
              max_active: float | None = None,
              off_assets: float = 0.0,
              total_prev: float = 1.0,
              turnover_caused: bool = False) -> EnhancedIndexingResult:
    """不可行时退回一个**显式**候选，并如实报告它的违约情况。

    比「报错让上游崩」安全：一个违反约束的解会静默产生超出风险预算的暴露；
    退回上期持仓（换手最小）或基准（主动权重 0）都可解释、可交易。但退回
    的组合**不保证满足本次声明的全部约束** —— 上期持仓通常不是为本次 TE /
    主动上限准备的。因此本函数对候选逐条核验并把违约写进
    ``_constraint_violations``，绝不声称「已知可行」。

    退给谁：

    - ``turnover_caused``（求解环节失败且声明了 ``max_turnover``）：退回
      **上期原始持仓 w0** —— 退回基准的换手 ``Σ|b - w0| + off_assets`` 可能
      远超声明的上限，于是「刹车」恰恰在最该生效的时候缺席。
    - 其余（结构性边界矛盾、没给换手上限）：退回基准（主动权重全 0、TE 0）。

    换手照新口径如实计算（含现金/池外腿），不谎报 0；TE / 主动份额 /
    预期超额同样按真实候选计算。
    """
    prefer_prev = bool(turnover_caused and max_turnover is not None and w0 is not None)
    candidate = "prev" if prefer_prev else "benchmark"
    w = w0 if prefer_prev else b
    out = EnhancedIndexingResult({s: float(v) for s, v in zip(syms, w, strict=True)})
    out["_tracking_error"] = float(tracking_error(w, b, cov))
    out["_active_share"] = float(active_share(w, b))
    out["_expected_excess"] = float(alpha @ (w - b)) if alpha is not None else 0.0
    out["_cov_method"] = (cov_method or "sample") if candidate == "prev" else "n/a"
    out["_n_active"] = int((np.abs(w - b) > 1e-6).sum())
    if max_turnover is not None and w0 is not None:
        out["_turnover_from_prev"] = _true_turnover(w, w0, off_assets, total_prev)
    else:
        out["_turnover_from_prev"] = None
    violations = _constraint_violations(
        w, b, cov,
        te_target=0.0 if te_target is None else float(te_target),
        max_weight=max_weight, max_active=max_active,
        max_turnover=max_turnover, prev=w0,
        off_assets=off_assets, total_prev=total_prev)
    if candidate == "prev":
        reason = (f"{reason} → 保持上期持仓（换手上限 "
                  f"{float(max_turnover)} 下无可行解）")
    else:
        reason = f"{reason} → 退回基准（主动权重全 0）"
    if violations:
        reason = f"{reason}；注意：该解仍违反 {'；'.join(violations)}"
    out["_benchmark_source"] = benchmark_source
    out["_fallback"] = True
    out["_fallback_candidate"] = candidate
    out["_constraint_violations"] = violations
    out["_fallback_reason"] = reason
    return out
