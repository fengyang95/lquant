"""权重优化：等权 / 市值 / 逆波动 / 风险平价 / 最小方差 / HRP。

等权是最难被打败的基准 —— DeMiguel 等人证明过，在协方差估计误差面前，
大部分优化器的样本外表现不如 1/N。所以这里每个方法都能一键和等权对拍，
优化只在「有明确理由相信协方差估计可靠」时才用。

HRP（层次风险平价）是例外：它不做矩阵求逆，对估计误差最稳健，
A 股这种高相关、噪声大的市场里通常优于均值方差。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.portfolio.optimizer import OptimizerError

__all__ = ["equal_weight", "score_weight", "market_cap_weight", "inverse_vol_weight",
           "risk_parity_weight", "min_variance_weight", "hrp_weight",
           "weights", "METHODS", "weight_report"]


def _returns_matrix(returns, symbols: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    """接受宽表 DataFrame 或 ndarray，返回 (T×N 矩阵, 代码列表)。"""
    if isinstance(returns, pl.DataFrame):
        if symbols is None:
            symbols = [c for c in returns.columns
                       if c not in ("trade_date", "date", "symbol")]
        M = np.column_stack([returns[s].cast(pl.Float64).to_numpy() for s in symbols])
    else:
        M = np.asarray(returns, dtype=float)
        if symbols is None:
            symbols = [f"a{i}" for i in range(M.shape[1])]
    return np.nan_to_num(M, nan=0.0), list(symbols)


def _cov_for(M: np.ndarray, cov_method: str | None, cov_params: dict | None) -> np.ndarray:
    """按 ``cov_method`` 估计协方差；None/``sample`` 用样本协方差。

    这是风险模型（``portfolio/riskmodel.py``）接入权重层的入口。默认仍是
    样本协方差 —— 保持既有行为不变（1/N 与样本口径是既有回归基准），
    要用收缩/因子模型必须**显式**指定，避免静默换口径。
    """
    if not cov_method or cov_method == "sample":
        return np.atleast_2d(np.cov(M, rowvar=False))
    from lquant.portfolio.riskmodel import estimate_cov

    return estimate_cov(M, cov_method, **(cov_params or {}))


def _clean(w: np.ndarray) -> np.ndarray:
    w = np.nan_to_num(w, nan=0.0, posinf=0.0, neginf=0.0)
    w = np.clip(w, 0.0, None)
    s = w.sum()
    return w / s if s > 1e-12 else np.ones(len(w)) / len(w)


def equal_weight(returns, symbols: list[str] | None = None, **kw) -> dict[str, float]:
    """1/N。样本外最稳健的基准，所有优化结果都该先和它比。"""
    _, syms = _returns_matrix(returns, symbols)
    n = len(syms)
    return dict(zip(syms, [1.0 / n] * n, strict=False))


def score_weight(df: pl.DataFrame, score_col: str = "score", *,
                 symbol_col: str = "symbol", positive_only: bool = True) -> dict[str, float]:
    """按分数加权（分数归一化到正值后作权重）。"""
    d = df.select([symbol_col, score_col]).drop_nulls()
    if not len(d):
        return {}
    s = d[score_col].cast(pl.Float64).to_numpy()
    if positive_only:
        s = np.clip(s, 0.0, None)
    if s.sum() <= 0:
        return {d[symbol_col][i]: 1.0 / len(d) for i in range(len(d))}
    return {d[symbol_col][i]: float(s[i] / s.sum()) for i in range(len(d))}


def market_cap_weight(df: pl.DataFrame, cap_col: str = "market_cap", *,
                      symbol_col: str = "symbol", sqrt: bool = False) -> dict[str, float]:
    """市值加权。sqrt=True 时用平方根市值，介于等权与市值加权之间。"""
    d = df.select([symbol_col, cap_col]).drop_nulls()
    if not len(d):
        return {}
    c = d[cap_col].cast(pl.Float64).to_numpy()
    if sqrt:
        c = np.sqrt(np.clip(c, 0, None))
    return {d[symbol_col][i]: float(v) for i, v in enumerate(_clean(c))}


def inverse_vol_weight(returns, symbols: list[str] | None = None,
                       **kw) -> dict[str, float]:
    """逆波动率加权。风险平价在低相关组合下的一阶近似，几乎不需要协方差估计。"""
    M, syms = _returns_matrix(returns, symbols)
    vol = M.std(axis=0, ddof=1)
    vol[vol < 1e-9] = 1e-9
    return dict(zip(syms, _clean(1.0 / vol), strict=False))


def risk_parity_weight(returns, symbols: list[str] | None = None, *,
                       max_iter: int = 500, tol: float = 1e-9,
                       cov_method: str | None = None,
                       cov_params: dict | None = None, **kw) -> dict[str, float]:
    """等风险贡献（ERC）：每只票对组合风险的贡献相同。

    用 SLSQP 最小化风险贡献与目标值的偏离，n 较大时慢，建议 ≤ 100 只。
    优化失败（协方差奇异等）时降级为逆波动率 —— 它本来就是 ERC 的近似解。

    ``cov_method``：协方差估计口径（``sample`` 默认；``shrink_lw``/``poet`` 等
    见 ``portfolio.riskmodel.COV_ESTIMATORS``）。T<N 时样本协方差奇异，
    ERC 会退化成逆波动率；显式指定收缩口径可以避免这次静默降级。
    """
    M, syms = _returns_matrix(returns, symbols)
    n = len(syms)
    if n < 2 or M.shape[0] < 3:
        return inverse_vol_weight(M, syms)
    cov = _cov_for(M, cov_method, cov_params)
    try:
        from scipy.optimize import minimize

        def risk_contrib(w: np.ndarray) -> np.ndarray:
            sw = cov @ w
            port_var = float(w @ sw)
            if port_var <= 1e-16:
                return np.ones(n) / n
            return w * sw / port_var

        def obj(w: np.ndarray) -> float:
            rc = risk_contrib(w)
            return float(((rc - rc.mean()) ** 2).sum())

        x0 = _clean(1.0 / np.clip(M.std(axis=0, ddof=1), 1e-9, None))
        res = minimize(obj, x0, method="SLSQP",
                       bounds=[(1e-6, 1.0)] * n,
                       constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
                       options={"maxiter": max_iter, "ftol": tol})
        if not res.success or not np.all(np.isfinite(res.x)):
            raise RuntimeError("risk parity 未收敛")
        return dict(zip(syms, _clean(res.x), strict=False))
    except Exception:
        return inverse_vol_weight(M, syms)


def min_variance_weight(returns, symbols: list[str] | None = None, *,
                        max_weight: float = 1.0,
                        cov_method: str | None = None,
                        cov_params: dict | None = None, **kw) -> dict[str, float]:
    """最小方差组合。对协方差估计误差最敏感，慎用。

    ``cov_method``：协方差口径（见 ``portfolio.riskmodel``）。样本协方差在
    T<N 时奇异 → 权重由数值噪声决定；指定 ``shrink_lw`` 或 ``structured_pca``
    才能让这个优化器真正可用。
    """
    M, syms = _returns_matrix(returns, symbols)
    n = len(syms)
    if n < 2 or M.shape[0] < 3:
        return equal_weight(M, syms)
    cov = _cov_for(M, cov_method, cov_params)
    try:
        from scipy.optimize import minimize

        res = minimize(lambda w: float(w @ cov @ w), np.ones(n) / n, method="SLSQP",
                       bounds=[(0.0, max_weight)] * n,
                       constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
                       options={"maxiter": 300, "ftol": 1e-12})
        if not res.success:
            raise RuntimeError("min variance 未收敛")
        return dict(zip(syms, _clean(res.x), strict=False))
    except Exception:
        return inverse_vol_weight(M, syms)


def hrp_weight(returns, symbols: list[str] | None = None, *,
               link: str = "single", **kw) -> dict[str, float]:
    """层次风险平价（de Prado）。不做矩阵求逆，对估计误差最稳健。

    步骤：相关距离 → 层次聚类 → 递归二分按方差分配权重。
    """
    M, syms = _returns_matrix(returns, symbols)
    n = len(syms)
    if n < 3 or M.shape[0] < 3:
        return inverse_vol_weight(M, syms)

    try:
        from scipy.cluster.hierarchy import leaves_list, linkage
        from scipy.spatial.distance import squareform
    except ImportError:
        return inverse_vol_weight(M, syms)

    cov = np.atleast_2d(np.cov(M, rowvar=False))
    vol = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov / np.outer(vol, vol)
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    corr = (corr + corr.T) / 2

    dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
    np.fill_diagonal(dist, 0.0)
    try:
        Z = linkage(squareform(dist, checks=False), method=link)
        order = leaves_list(Z)
    except Exception:
        order = np.arange(n)

    w = np.ones(n)
    order = np.asarray(order, dtype=int)

    def cluster_var(idx: np.ndarray) -> float:
        sub = cov[np.ix_(idx, idx)]
        iv = 1.0 / np.clip(np.diag(sub), 1e-12, None)
        ww = _clean(iv)
        return float(ww @ sub @ ww)

    # 逐层细分：每层把每个簇二分，按簇内方差反比分配权重
    clusters: list[np.ndarray] = [order]
    while clusters:
        nxt: list[np.ndarray] = []
        for c in clusters:
            if len(c) <= 1:
                continue
            half = len(c) // 2
            left, right = c[:half], c[half:]
            vl, vr = cluster_var(left), cluster_var(right)
            total = vl + vr
            alpha = 0.5 if total <= 1e-16 else 1.0 - vl / total
            w[left] *= alpha
            w[right] *= (1.0 - alpha)
            nxt.extend([left, right])
        clusters = nxt

    return dict(zip(syms, _clean(w), strict=False))


def _enhanced_indexing(returns, symbols=None, **kw):
    """``weighting.METHODS`` 适配器：基准相对优化。

    没给 ``benchmark_weights`` 时按**等权基准**处理（与 ``backtest.benchmark``
    的等权代理同一口径），这样通用入口 ``weights(method="enhanced_indexing")``
    也能调 —— 注册表里的方法必须都能被统一入口调用。是不是等权兜底看结果里的
    ``_benchmark_source``，别把它当真指数基准。
    """
    from lquant.portfolio.optimizer import enhanced_indexing_weight

    # 只回权重本身：结果对象还带 _ 前缀的诊断字段（含字符串），
    # 整个 dict 化会让权重字典混进非数值，调用方 sum() 直接炸。
    return enhanced_indexing_weight(returns, symbols, **kw).weights()


METHODS = {
    "equal": equal_weight,
    "inverse_vol": inverse_vol_weight,
    "risk_parity": risk_parity_weight,
    "min_variance": min_variance_weight,
    "hrp": hrp_weight,
    # 需要 benchmark_weights（见 portfolio/optimizer.py）
    "enhanced_indexing": _enhanced_indexing,
}


def weights(returns, method: str = "equal", symbols: list[str] | None = None,
            **kw) -> dict[str, float]:
    """统一入口。

    未知方法（多半是拼错的名字）退回等权而不是报错 —— 权重算不出来不该让
    整个流程崩。但**已注册方法**抛的错要照实往上抛：那通常是「缺了必需输入」
    （如 ``enhanced_indexing`` 没给 ``scores``/``expected_returns``），
    静默退回等权会让人以为约束真的生效了。这条不对称是刻意的。
    """
    if method not in METHODS:
        return equal_weight(returns, symbols)
    return METHODS[method](returns, symbols, **kw)


def weight_report(returns, symbols: list[str] | None = None,
                  methods: list[str] | None = None, **kw) -> pl.DataFrame:
    """各权重方案的对比：组合波动、有效持仓数（分散度）、**算不出来的原因**。

    需要额外输入的方法（``enhanced_indexing`` 要 α 视图，可用 ``scores=`` 传入）
    会在 ``note`` 里说明原因，对应指标留空 —— 报告要能列全注册方法，
    而不是因为其中一个缺输入就整体崩掉。
    """
    M, syms = _returns_matrix(returns, symbols)
    cov = np.atleast_2d(np.cov(M, rowvar=False)) if M.shape[0] > 2 and len(syms) > 1 \
        else np.eye(len(syms)) * 1e-8
    rows = []
    for name in (methods or list(METHODS)):
        try:
            w = np.array([weights(M, name, syms, **kw).get(s, 0.0) for s in syms])
        except (OptimizerError, ValueError) as e:
            # 需要额外输入的方法（enhanced_indexing 要 α 视图）不该让整张报告炸 ——
            # 记一行 note 说明为什么算不出来，报告仍能列全所有注册方法。
            rows.append({"method": name, "vol": None, "effective_n": None,
                         "max_weight": None, "n_holdings": None, "note": str(e)})
            continue
        vol = float(np.sqrt(w @ cov @ w))
        # 有效持仓数 = 1 / HHI，衡量分散度
        eff = float(1.0 / (w ** 2).sum()) if (w ** 2).sum() > 0 else 0.0
        rows.append({"method": name, "vol": vol, "effective_n": eff,
                     "max_weight": float(w.max()), "n_holdings": int((w > 1e-6).sum()),
                     "note": None})
    # nulls_last：算不出来的方法排到最后，而不是让 NaN 打乱波动率排序
    return pl.DataFrame(rows).sort("vol", nulls_last=True)
