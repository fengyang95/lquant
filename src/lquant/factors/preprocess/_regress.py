"""截面回归的内部工具：设计矩阵构造 + 逐日残差求解。

放在这里是因为中性化和正交化都要用，而且**逐日分组**这个坑只该踩一次：
- 绝不跨日期拟合（那是未来函数）
- 每天的样本数可能不足以支撑回归，要有降级路径
- 哑变量必须按全局 level 构造，否则每天列数不一致
"""
from __future__ import annotations

import numpy as np
import polars as pl

MISSING_CAT = "__NA__"


def split_levels(df: pl.DataFrame, cols: list[str]) -> tuple[list[str], list[str]]:
    """把因子列分成「数值列」和「分类列」（按 dtype 判定）。"""
    num, cat = [], []
    for c in cols:
        if c not in df.columns:
            continue
        if df.schema[c] in (pl.Utf8, pl.Categorical, pl.Enum, pl.Boolean):
            cat.append(c)
        else:
            num.append(c)
    return num, cat


def encode_cats(df: pl.DataFrame, cat_cols: list[str]
                ) -> tuple[pl.DataFrame, dict[str, list[str]], list[str]]:
    """把分类列编码成整数索引列，返回 (新df, {列: levels}, 编码列名)。

    levels 取**全局**唯一值（drop_first 以避免与截距列共线），
    这样每天的哑变量列数一致，矩阵运算才能向量化。
    """
    levels: dict[str, list[str]] = {}
    enc_cols: list[str] = []
    out = df
    for c in cat_cols:
        vals = (out[c].cast(pl.Utf8, strict=False).fill_null(MISSING_CAT).unique().sort().to_list())
        levels[c] = vals[1:] if len(vals) > 1 else vals    # drop first
        enc = f"__{c}_idx"
        mapping = {v: i for i, v in enumerate(levels[c])}
        out = out.with_columns(
            pl.col(c).cast(pl.Utf8, strict=False).fill_null(MISSING_CAT)
            .replace_strict(mapping, default=len(levels[c])).alias(enc)
        )
        enc_cols.append(enc)
    return out, levels, enc_cols


def design_matrix(sub: pl.DataFrame, num_cols: list[str],
                  cat_levels: dict[str, list[str]], enc_cols: list[str]) -> np.ndarray:
    """构造 [1, 数值列..., 哑变量...] 的设计矩阵。"""
    n = len(sub)
    blocks = [np.ones((n, 1))]
    for c in num_cols:
        v = sub[c].cast(pl.Float64, strict=False).to_numpy()
        v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
        blocks.append(v.reshape(n, 1))
    for c, enc in zip(cat_levels.keys(), enc_cols):
        k = len(cat_levels[c])
        if k == 0:
            continue
        idx = np.nan_to_num(sub[enc].cast(pl.Float64, strict=False).to_numpy(), nan=k).astype(int)
        mat = np.zeros((n, k))
        in_range = (idx >= 0) & (idx < k)
        mat[np.arange(n)[in_range], idx[in_range]] = 1.0
        blocks.append(mat)
    return np.hstack(blocks)


def valid_mask(sub: pl.DataFrame, col: str, num_cols: list[str]) -> np.ndarray:
    """y 或数值协变量缺失的行不参与回归。"""
    y = sub[col].cast(pl.Float64, strict=False).to_numpy()
    m = ~np.isnan(y) & ~np.isinf(y)
    for c in num_cols:
        v = sub[c].cast(pl.Float64, strict=False).to_numpy()
        m &= ~np.isnan(v)
    return m


def residual_by_day(
    df: pl.DataFrame,
    col: str,
    by: str,
    factor_cols: list[str],
    solve,
    min_obs: int | None = None,
) -> pl.DataFrame:
    """逐日回归并写回残差。

    Parameters
    ----------
    solve : (X, y) -> (beta, resid_on_valid)
        回归求解器，只处理有效行。
    """
    num_cols, cat_cols = split_levels(df, factor_cols)
    enc_df, levels, enc_cols = encode_cats(df, cat_cols)
    need = len(num_cols) + sum(len(v) for v in levels.values()) + 2
    min_obs = min_obs or need

    out = enc_df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias(col))
    values = out[col].to_numpy().astype(float)
    cursor = 0
    for sub in out.partition_by(by, as_dict=False, maintain_order=True):
        n = len(sub)
        X = design_matrix(sub, num_cols, levels, enc_cols)
        y = sub[col].to_numpy().astype(float)
        m = valid_mask(sub, col, num_cols)

        if m.sum() < min_obs:
            # 样本不足以支撑回归 → 退化为去均值，至少去掉整体水平
            resid = np.where(m, y - np.nanmean(y[m]) if m.any() else 0.0, np.nan)
        else:
            Xv, yv = X[m], y[m]
            resid_v = solve(Xv, yv)
            resid = np.full(n, np.nan)
            resid[m] = resid_v
        values[cursor : cursor + n] = resid
        cursor += n

    return enc_df.drop(enc_cols).with_columns(pl.Series(col, values))


def ols_resid(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return y - X @ beta


def ridge_resid(X: np.ndarray, y: np.ndarray, lam: float = 1e-3) -> np.ndarray:
    """岭回归闭式解。不对截距列惩罚（第 0 列）。"""
    p = X.shape[1]
    reg = np.eye(p) * lam
    reg[0, 0] = 0.0
    beta = np.linalg.solve(X.T @ X + reg, X.T @ y)
    return y - X @ beta
