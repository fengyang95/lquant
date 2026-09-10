"""中性化：OLS / Ridge / Lasso（市值 + 申万一级）。

不做中性化的市值因子，本质上就是小市值因子换个名字 ——
A 股小市值暴露能解释掉大部分「有效因子」的收益，这是最经典的假信号。

用法：neutralize(df, "bp", by=["market_cap", "industry_sw1"])
其中连续列直接进设计矩阵，字符串列自动展开成哑变量（drop-first 避免共线）。
"""
from __future__ import annotations

import numpy as np
import polars as pl
from loguru import logger

from lquant.factors.preprocess._regress import ols_resid, residual_by_day, ridge_resid
from lquant.factors.preprocess.registry import method

# 市值暴露列：spec §2.4 —— float_mv（日线湖）有值时优先，null 行回退
# 旧路径（财务/外部推的 market_cap）。market_cap_source 可强制指定。
CAP_COL = "market_cap"
FLOAT_MV_COL = "float_mv"
_CAP_EFF = "__market_cap_eff"


def _resolve_market_cap(
    df: pl.DataFrame, factors: list[str], source: str,
) -> tuple[pl.DataFrame, list[str], str | None]:
    """按 market_cap_source 解析市值暴露列。

    返回 (新 df, 新 factors, 临时列名或 None)。临时列在回归后由调用方 drop，
    输入 df 不被修改（immutability）。
    """
    if source == "market_cap" or CAP_COL not in factors:
        return df, factors, None
    if source == "float_mv" and FLOAT_MV_COL not in df.columns:
        raise ValueError(
            f"market_cap_source='float_mv' 但 df 无 {FLOAT_MV_COL} 列")
    if source == "auto" and FLOAT_MV_COL not in df.columns:
        return df, factors, None
    if source not in ("auto", "float_mv"):
        raise ValueError(f"未知 market_cap_source: {source}")
    _warn_unit_mismatch(df)
    out = df.with_columns(
        pl.coalesce(pl.col(FLOAT_MV_COL), pl.col(CAP_COL)).alias(_CAP_EFF)
    )
    return out, [_CAP_EFF if f == CAP_COL else f for f in factors], _CAP_EFF


# float_mv/market_cap 比率合理区间：两列都应代表流通/总市值（同一量纲，元），
# 比率显著越界说明某一列单位错了（如手写成了亿/万元），coalesce 会污染暴露。
_UNIT_RATIO_LO, _UNIT_RATIO_HI = 0.05, 1.2


def _warn_unit_mismatch(df: pl.DataFrame) -> None:
    """两列均有非空值时，按中位数比率检查量纲一致性；只告警不改数据。"""
    if FLOAT_MV_COL not in df.columns or CAP_COL not in df.columns:
        return
    sub = df.select(FLOAT_MV_COL, CAP_COL).drop_nulls()
    if sub.height == 0:
        return
    ratio = (sub[FLOAT_MV_COL] / sub[CAP_COL]).median()
    if ratio is None:
        return
    if not (_UNIT_RATIO_LO <= ratio <= _UNIT_RATIO_HI):
        logger.warning(
            "float_mv 与 market_cap 量纲疑似不一致"
            f"（比率 {ratio:.2f}），市值暴露可能被污染")


def _neutralize_residuals(df, col, by, factors, *, source, solve):
    """公共路径：解析市值暴露 → 逐日回归 → 清理临时列。"""
    df2, facs, eff = _resolve_market_cap(df, factors, source)
    out = residual_by_day(df2, col, by, facs, solve)
    return out.drop(eff) if eff else out


@method("ols", stage="neutralize", label="OLS 中性化",
        params={"factors": ["market_cap", "industry_sw1"],
                "market_cap_source": "auto"})
def ols(df: pl.DataFrame, col: str, *, by: str = "trade_date",
        factors: list[str] | None = None,
        market_cap_source: str = "auto", **kw) -> pl.DataFrame:
    """普通最小二乘残差。默认对市值 + 申万一级行业中性。"""
    return _neutralize_residuals(df, col, by,
                                 factors or [CAP_COL, "industry_sw1"],
                                 source=market_cap_source, solve=ols_resid)


@method("ridge", stage="neutralize", label="Ridge 中性化",
        params={"factors": ["market_cap", "industry_sw1"],
                "market_cap_source": "auto", "lam": 1e-3})
def ridge(df: pl.DataFrame, col: str, *, by: str = "trade_date",
          factors: list[str] | None = None, lam: float = 1e-3,
          market_cap_source: str = "auto", **kw) -> pl.DataFrame:
    """岭回归。行业哑变量多、样本少时比 OLS 稳。"""
    return _neutralize_residuals(df, col, by,
                                 factors or [CAP_COL, "industry_sw1"],
                                 source=market_cap_source,
                                 solve=lambda X, y: ridge_resid(X, y, lam))


@method("lasso", stage="neutralize", label="Lasso 中性化",
        params={"factors": ["market_cap", "industry_sw1"],
                "market_cap_source": "auto", "alpha": 1e-4})
def lasso(df: pl.DataFrame, col: str, *, by: str = "trade_date",
          factors: list[str] | None = None, alpha: float = 1e-4,
          market_cap_source: str = "auto", **kw) -> pl.DataFrame:
    """Lasso。行业数量多时自动稀疏化，未装 sklearn 时降级为 Ridge。"""
    try:
        from sklearn.linear_model import Lasso
    except ImportError:
        return ridge(df, col, by=by, factors=factors, lam=alpha,
                     market_cap_source=market_cap_source)

    model = Lasso(alpha=alpha, fit_intercept=False, max_iter=2000)

    def solve(X: np.ndarray, y: np.ndarray) -> np.ndarray:
        # X 第 0 列是截距，Lasso 不能惩罚它 —— 先中心化再拟合
        y0 = X[:, 0] @ [y.mean()]
        yc = y - y[:1].mean()
        model.fit(X[:, 1:], yc)
        return y - (y0 + X[:, 1:] @ model.coef_)

    return _neutralize_residuals(df, col, by,
                                 factors or [CAP_COL, "industry_sw1"],
                                 source=market_cap_source, solve=solve)


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
