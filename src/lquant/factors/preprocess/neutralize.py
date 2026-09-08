"""中性化：OLS / Ridge / Lasso（市值 + 申万一级）。

不做中性化的市值因子，本质上就是小市值因子换个名字 ——
A 股小市值暴露能解释掉大部分「有效因子」的收益，这是最经典的假信号。

用法：neutralize(df, "bp", by=["market_cap", "industry_sw1"])
其中连续列直接进设计矩阵，字符串列自动展开成哑变量（drop-first 避免共线）。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.factors.preprocess._regress import ols_resid, residual_by_day, ridge_resid
from lquant.factors.preprocess.registry import method


@method("ols", stage="neutralize", label="OLS 中性化",
        params={"factors": ["market_cap", "industry_sw1"]})
def ols(df: pl.DataFrame, col: str, *, by: str = "trade_date",
        factors: list[str] | None = None, **kw) -> pl.DataFrame:
    """普通最小二乘残差。默认对市值 + 申万一级行业中性。"""
    return residual_by_day(df, col, by, factors or ["market_cap", "industry_sw1"], ols_resid)


@method("ridge", stage="neutralize", label="Ridge 中性化",
        params={"factors": ["market_cap", "industry_sw1"], "lam": 1e-3})
def ridge(df: pl.DataFrame, col: str, *, by: str = "trade_date",
          factors: list[str] | None = None, lam: float = 1e-3, **kw) -> pl.DataFrame:
    """岭回归。行业哑变量多、样本少时比 OLS 稳。"""
    return residual_by_day(df, col, by, factors or ["market_cap", "industry_sw1"],
                           lambda X, y: ridge_resid(X, y, lam))


@method("lasso", stage="neutralize", label="Lasso 中性化",
        params={"factors": ["market_cap", "industry_sw1"], "alpha": 1e-4})
def lasso(df: pl.DataFrame, col: str, *, by: str = "trade_date",
          factors: list[str] | None = None, alpha: float = 1e-4, **kw) -> pl.DataFrame:
    """Lasso。行业数量多时自动稀疏化，未装 sklearn 时降级为 Ridge。"""
    try:
        from sklearn.linear_model import Lasso
    except ImportError:
        return ridge(df, col, by=by, factors=factors, lam=alpha)

    model = Lasso(alpha=alpha, fit_intercept=False, max_iter=2000)

    def solve(X: np.ndarray, y: np.ndarray) -> np.ndarray:
        # X 第 0 列是截距，Lasso 不能惩罚它 —— 先中心化再拟合
        y0 = X[:, 0] @ [y.mean()]
        yc = y - y[:1].mean()
        model.fit(X[:, 1:], yc)
        return y - (y0 + X[:, 1:] @ model.coef_)

    return residual_by_day(df, col, by, factors or ["market_cap", "industry_sw1"], solve)


@method("industry_mean", stage="neutralize", label="行业均值剔除",
        params={"factors": ["industry_sw1"]})
def industry_mean(df: pl.DataFrame, col: str, *, by: str = "trade_date",
                  factors: list[str] | None = None, group: str = "industry_sw1", **kw) -> pl.DataFrame:
    """组内去均值。比回归简单粗暴，但在行业暴露上已经能去掉大部分。

    作为 OLS 的对照：如果两者结果差异很大，说明「行业」之外还有市值等连续暴露。
    """
    if group not in df.columns:
        return df
    return df.with_columns(
        (pl.col(col) - pl.col(col).mean().over([by, group])).alias(col)
    )


@method("none", stage="neutralize", label="不中性化")
def none(df: pl.DataFrame, col: str, *, by: str = "trade_date", **kw) -> pl.DataFrame:
    return df
