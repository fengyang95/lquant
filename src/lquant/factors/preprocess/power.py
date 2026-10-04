"""幂变换：Box-Cox / Yeo-Johnson（截面口径，变换后紧跟截面 Z-Score）。

用途：把右偏/重尾的因子拉回近似正态，再标准化 —— 线性模型（IC 加权、回归中性化）
对偏度敏感，这一步常能把 IC 提一点。两者都是**单调变换**，所以不改 Rank IC
（只改 IC 的数值尺度与线性模型的拟合质量）；要的是「排序不变但分布更友好」就用它，
要抗极值请用 winsorize 的 rankgauss。

**为什么跟着 Z-Score 而不能只做变换**：幂变换后的量纲随 λ 漂移（λ=0 时是 log），
不标准化没法在多因子加权里直接用。本仓 standardize 阶段一个方法只能做一件事，
所以这里把「变换 + 截面 Z」合成一个方法（与 AlphaPurify 同名方法一致）。

**修正了 AlphaPurify 的前视泄漏**：它的 ``boxcox_standardize`` 取**全样本**
（含未来日期）最小值做平移 ``shift = -min + eps`` 再逐日变换 —— 因子值会随新数据
到来整体漂移，回测里等于用了未来信息。这里把平移**限制在当日截面**
（``min().over(by)``），只用当日可见信息。两者坐标原点因此不同：交叉验证时应按当日
截面口径重算，或直接比变换后的 z 值（平移在 z 空间被消掉）。
"""
from __future__ import annotations

import math

import polars as pl

from lquant.factors.preprocess.registry import method

# 与 rolling.py 同理：复用既有的「σ≈0 → 兜底 1.0」约定，不抄第三份实现。
from lquant.factors.preprocess.standardize import _safe_std

__all__ = ["boxcox", "yeo_johnson"]


def _check_lambda(lambda_: float, name: str) -> float:
    try:
        lam = float(lambda_)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 的 lambda_ 必须是数值，收到 {lambda_!r}") from e
    if not math.isfinite(lam):
        raise ValueError(f"{name} 的 lambda_ 必须是有限数，收到 {lambda_!r}")
    return lam


def _cross_z(df: pl.DataFrame, col: str, by: str) -> pl.DataFrame:
    """截面 Z-Score（与 standardize.zscore 同口径：零方差 → 0）。"""
    mean = pl.col(col).mean().over(by)
    std = _safe_std(pl.col(col).std().over(by))
    return df.with_columns(((pl.col(col) - mean) / std).alias(col))


@method("boxcox", stage="standardize", label="Box-Cox + Z",
        params={"lambda_": 0.0, "eps": 1e-9},
        formula="按当日截面平移使 min>0：x̃ = x − min_by + eps；"
                "λ=0 → log(x̃)，否则 (x̃^λ − 1)/λ；再截面 Z-Score",
        notes=(
            "Box-Cox 要求 x>0，所以先按**当日截面**最小值平移"
            "（AlphaPurify 用全样本最小值，含未来日期，属前视泄漏 —— 见模块 docstring）。"
            "λ=0 是 log 特例；λ=1 只差平移与尺度，Z 之后与恒等等价。"
            "正偏重尾因子常用 λ∈[0, 0.5]。"
            "**只对量纲一致的因子讲得通**：平移量随当日最小值走，"
            "若一列里混了不同量纲（如 vol 与 mv），λ 的物理意义不成立 —— "
            "那种场景请按量纲分组分别变换。"
        ),
        zero_variance="平移后仍全截面同值 → Z 的 σ≈0 兜底 1.0 → 输出 0。")
def boxcox(df: pl.DataFrame, col: str, *, by: str = "trade_date",
           lambda_: float = 0.0, eps: float = 1e-9) -> pl.DataFrame:
    """Box-Cox 幂变换 + 截面 Z-Score。细节见 registry 的 notes。"""
    lam = _check_lambda(lambda_, "boxcox")
    if eps <= 0:
        raise ValueError(f"eps 必须为正，收到 {eps}")
    # 当日截面平移：x − min + eps > 0（eps 保证最小值不为 0）
    x = pl.col(col) - pl.col(col).min().over(by) + eps
    pt = x.log() if lam == 0 else (x.pow(lam) - 1) / lam
    return _cross_z(df.with_columns(pt.alias(col)), col, by)


@method("yeo_johnson", stage="standardize", label="Yeo-Johnson + Z",
        params={"lambda_": 0.0},
        formula="x≥0: λ=0 → log(x+1)，否则 ((x+1)^λ − 1)/λ；"
                "x<0: λ=2 → −log(1−x)，否则 −(((1−x)^(2−λ) − 1)/(2−λ))；再截面 Z-Score",
        notes=(
            "Box-Cox 的推广，**接受负数与零**（无需平移），因此更适合带正负号的因子"
            "（动量差、超额收益、利差）。λ=0 时上半支是 log1p、下半支是等价的"
            "−log(1−x) 形式，在 0 处连续可导。"
            "口径与 AlphaPurify yeo_johnson_standardize 一致，唯一差异是它在分母加 eps"
            "（σ+eps），本仓用 σ≈0 兜底 1.0 的既有约定。"
        ),
        zero_variance="与 boxcox 同：截面 σ≈0 时输出 0。")
def yeo_johnson(df: pl.DataFrame, col: str, *, by: str = "trade_date",
                lambda_: float = 0.0) -> pl.DataFrame:
    """Yeo-Johnson 幂变换 + 截面 Z-Score（接受负数）。"""
    lam = _check_lambda(lambda_, "yeo_johnson")
    x = pl.col(col)
    pos = (x + 1).log() if lam == 0 else ((x + 1).pow(lam) - 1) / lam
    neg = -(1 - x).log() if lam == 2 else -(((1 - x).pow(2 - lam) - 1) / (2 - lam))
    # `x >= 0` 遇 null 得 null → when 落到 otherwise（SQL 语义），
    # 但两支都建立在 null 上，结果仍是 null，不会被替换成别的值。
    pt = pl.when(x >= 0).then(pos).otherwise(neg)
    return _cross_z(df.with_columns(pt.alias(col)), col, by)
