"""相关性去重：高度相关的标的只保留一个。

Top50 选股里如果有 8 只券商股，名义上分散了 50 只，
实际暴露的是一个行业 —— 风险被严重低估，一次行业回调全军覆没。

做法是贪心聚类：按「代表性」排序（默认流动性），
依次把与已选标的相关性超过阈值的归入同一簇，簇内只留第一个。
贪心不是最优解，但 O(n²) 且结果稳定可复现，足够用。
"""
from __future__ import annotations

import numpy as np
import polars as pl

__all__ = ["correlation_matrix", "dedup", "cluster_labels", "cluster_summary"]


def correlation_matrix(returns: pl.DataFrame | np.ndarray,
                       symbols: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    """收益序列的相关矩阵。样本不足时退回单位阵。"""
    if isinstance(returns, pl.DataFrame):
        if symbols is None:
            symbols = [c for c in returns.columns
                       if c not in ("trade_date", "date", "symbol")]
        M = np.column_stack([returns[s].cast(pl.Float64).to_numpy() for s in symbols])
    else:
        M = np.asarray(returns, dtype=float)
        symbols = symbols or [f"a{i}" for i in range(M.shape[1])]
    M = np.nan_to_num(M, nan=0.0)
    if M.shape[0] < 3:
        return np.eye(len(symbols)), list(symbols)
    with np.errstate(divide="ignore", invalid="ignore"):
        c = np.corrcoef(M, rowvar=False)
    c = np.atleast_2d(np.nan_to_num(c, nan=0.0))
    np.fill_diagonal(c, 1.0)
    return np.clip(c, -1.0, 1.0), list(symbols)


def dedup(returns, symbols: list[str] | None = None, *,
          threshold: float = 0.9, priority: pl.DataFrame | dict[str, float] | None = None,
          priority_col: str = "amount", symbol_col: str = "symbol") -> list[str]:
    """贪心去重，返回保留下来的代码列表。

    Parameters
    ----------
    threshold : 相关系数阈值，超过即视为同质
    priority : 代表性排序依据。给 DataFrame 时按 priority_col 降序（流动性优先）；
               给 dict 时按值降序；None 则按输入顺序
    """
    corr, syms = correlation_matrix(returns, symbols)
    n = len(syms)
    if n <= 1:
        return syms

    order = _priority_order(syms, priority, priority_col, symbol_col)
    kept: list[int] = []
    for i in order:
        if all(abs(corr[i, j]) < threshold for j in kept):
            kept.append(i)
    # 保持优先序输出
    return [syms[i] for i in kept]


def _priority_order(syms: list[str], priority, priority_col: str,
                    symbol_col: str) -> list[int]:
    if priority is None:
        return list(range(len(syms)))
    if isinstance(priority, dict):
        return sorted(range(len(syms)), key=lambda i: -priority.get(syms[i], 0.0))
    d = priority.select([symbol_col, priority_col]).drop_nulls()
    if not len(d):
        return list(range(len(syms)))
    rank = {d[symbol_col][i]: float(d[priority_col][i]) for i in range(len(d))}
    return sorted(range(len(syms)), key=lambda i: -rank.get(syms[i], 0.0))


def cluster_labels(returns, symbols: list[str] | None = None, *,
                   threshold: float = 0.9, **kw) -> dict[str, int]:
    """给每个标的打簇标签（不丢弃，只标记）。需要先看结构再决定去留时用这个。"""
    corr, syms = correlation_matrix(returns, symbols)
    n = len(syms)
    labels: dict[str, int] = {}
    reps: list[int] = []
    cluster_id = 0
    for i in range(n):
        found = None
        for r in reps:
            if abs(corr[i, r]) >= threshold:
                found = r
                break
        if found is None:
            reps.append(i)
            labels[syms[i]] = cluster_id
            cluster_id += 1
        else:
            labels[syms[i]] = labels[syms[found]]
    return labels


def cluster_summary(returns, symbols: list[str] | None = None, *,
                    threshold: float = 0.9, **kw) -> pl.DataFrame:
    """簇结构概览：每簇有几个标的、代表是谁。"""
    labels = cluster_labels(returns, symbols, threshold=threshold, **kw)
    if not labels:
        return pl.DataFrame()
    rows: dict[int, list[str]] = {}
    for s, c in labels.items():
        rows.setdefault(c, []).append(s)
    return pl.DataFrame({
        "cluster": list(rows.keys()),
        "size": [len(v) for v in rows.values()],
        "members": [", ".join(sorted(v)) for v in rows.values()],
    }).sort("size", descending=True)
