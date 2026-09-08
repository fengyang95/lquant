"""正交化：施密特对称正交 / 逐步回归 / PCA。

因子之间高度相关时（比如一堆动量类因子），等权合成等于给同一个信号加了 N 倍权重，
风险被严重低估。正交化就是把共线性剥掉。

三种方法的取舍：
- symmetric（推荐）：对称处理，结果与因子顺序无关
- gram_schmidt：逐步回归，结果与输入顺序有关（先入为主），但可解释性强
- pca：彻底正交，但主成分失去了原始因子的经济含义
"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.factors.preprocess._regress import ols_resid
from lquant.factors.preprocess.registry import method

__all__ = ["symmetric", "gram_schmidt", "pca", "none"]


def _iter_days(df: pl.DataFrame, cols: list[str], by: str):
    """逐日取出 (mask, 因子矩阵)。缺失任一因子的行不参与。"""
    out = df.select([by, *cols]).with_columns(
        [pl.col(c).cast(pl.Float64, strict=False) for c in cols]
    )
    for sub in out.partition_by(by, as_dict=False, maintain_order=True):
        M = np.column_stack([sub[c].to_numpy().astype(float) for c in cols])
        m = np.all(np.isfinite(M), axis=1)
        yield sub, M, m


@method("symmetric", stage="orthogonalize", label="对称正交", params={"order_independent": True})
def symmetric(df: pl.DataFrame, cols: list[str], *, by: str = "trade_date") -> pl.DataFrame:
    """施密特对称正交（Schweinler-Wigner）。

    对 n×k 的 X 求 M = X'X，特征分解 M = U D U'，变换 S = U D^{-1/2} U'，
    则 X* = X S 满足 X*' X* = I。
    相比逐步回归，它对所有因子一视同仁，不受输入顺序影响。
    """
    if len(cols) < 2:
        return df
    res = {c: np.full(len(df), np.nan) for c in cols}
    cursor = 0
    for sub, M, m in _iter_days(df, cols, by):
        n = len(sub)
        X = M[m]
        if len(X) <= len(cols):
            cursor += n
            continue
        Xc = X - X.mean(axis=0)
        sd = Xc.std(axis=0)
        sd[sd < 1e-12] = 1.0
        Z = Xc / sd
        try:
            d, U = np.linalg.eigh(Z.T @ Z)
            d = np.clip(d, 1e-12, None)
            S = U @ np.diag(d ** -0.5) @ U.T
            Xs = Z @ S
        except np.linalg.LinAlgError:
            Xs = Z
        for j, c in enumerate(cols):
            res[c][cursor : cursor + n][m] = Xs[:, j]
        cursor += n
    return df.with_columns([pl.Series(c, res[c]) for c in cols])


@method("gram_schmidt", stage="orthogonalize", label="逐步回归正交",
        params={"order_independent": False})
def gram_schmidt(df: pl.DataFrame, cols: list[str], *, by: str = "trade_date") -> pl.DataFrame:
    """按顺序：第 i 个因子对前 i-1 个已正交因子做回归，取残差。

    结果与输入顺序有关 —— 这是它的缺点，也是它的用途：
    把最信任的因子放前面，后面的因子只保留增量信息。
    """
    if len(cols) < 2:
        return df
    res = {c: np.full(len(df), np.nan) for c in cols}
    cursor = 0
    for sub, M, m in _iter_days(df, cols, by):
        n = len(sub)
        X = M[m]
        if len(X) <= len(cols):
            cursor += n
            continue
        done: list[np.ndarray] = []
        for j in range(len(cols)):
            y = X[:, j]
            if done:
                D = np.column_stack([np.ones(len(X)), *done])
                y = ols_resid(D, y)
            done.append(y)
            res[cols[j]][cursor : cursor + n][m] = y
        cursor += n
    return df.with_columns([pl.Series(c, res[c]) for c in cols])


@method("pca", stage="orthogonalize", label="主成分正交", params={"n_components": None})
def pca(df: pl.DataFrame, cols: list[str], *, by: str = "trade_date",
        n_components: int | None = None, prefix: str = "pc") -> pl.DataFrame:
    """主成分替换。输出新列 pc_0..pc_{k-1}，彼此严格正交。

    代价是失去经济含义 —— 一般用于降维，不用于因子合成。
    """
    if len(cols) < 2:
        return df
    k = n_components or len(cols)
    mats = {i: np.full(len(df), np.nan) for i in range(k)}
    cursor = 0
    for sub, M, m in _iter_days(df, cols, by):
        n = len(sub)
        X = M[m]
        if len(X) <= len(cols):
            cursor += n
            continue
        Z = (X - X.mean(axis=0)) / np.clip(X.std(axis=0), 1e-12, None)
        try:
            _, _, Vt = np.linalg.svd(Z, full_matrices=False)
            comp = Z @ Vt[:k].T
        except np.linalg.LinAlgError:
            comp = Z[:, :k]
        for i in range(k):
            mats[i][cursor : cursor + n][m] = comp[:, i]
        cursor += n
    return df.with_columns([pl.Series(f"{prefix}_{i}", mats[i]) for i in range(k)])


@method("none", stage="orthogonalize", label="不正交化")
def none(df: pl.DataFrame, cols: list[str], *, by: str = "trade_date") -> pl.DataFrame:
    return df
