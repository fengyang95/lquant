"""截面异常收益过滤（filter_zscore）。

口径移植自 alphalens / ferric-alpha 的 `filter_zscore`：对每个交易日的前瞻收益
做截面 z-score，剔除 |z| > 阈值的**整行**。

为什么是剔除而不是 winsorize：A 股的极端前瞻收益大多不是信号而是数据噪声 ——
长期停牌复牌、退市整理期首日、重组复牌补跌，这些收益事后看很"极端"，
事前无法交易。留在样本里，IC 均值会被少数几只票左右（一个 +300% 就能
把某天的截面相关性拉飞）。

默认阈值 20（与 alphalens 默认一致）：正态下 |z|>20 的概率约 5e-89，
日常几乎不删行，只在真正的数据异常处兜底。想看它对结果的影响，
把它调小（如 3~5）当敏感性测试用 —— 如果因子结论立刻反转，
说明因子靠的就是这几只异常票。

零方差的截面（全停牌等）不参与判定，避免除零把整天的行全删掉。
"""
from __future__ import annotations

import polars as pl

__all__ = ["filter_zscore", "zscore_filter_stats"]

_DEFAULT_PREFIX = "fwd_ret"


def _resolve_cols(df: pl.DataFrame, cols: list[str] | None) -> list[str]:
    if cols is not None:
        miss = [c for c in cols if c not in df.columns]
        if miss:
            raise KeyError(f"缺少列 {miss}")
        return list(cols)
    out = [c for c in df.columns if c.startswith(_DEFAULT_PREFIX)]
    if not out:
        raise KeyError(f"未指定 cols 且找不到以 '{_DEFAULT_PREFIX}' 开头的收益列")
    return out


def _bad_mask(cols: list[str], date_col: str, threshold: float) -> pl.Expr:
    """任一收益列在该日截面 |z| > threshold → True。零方差截面不判定。"""
    masks = []
    for c in cols:
        std = pl.col(c).std(ddof=0).over(date_col)
        z = (pl.col(c) - pl.col(c).mean().over(date_col)) / pl.when(std > 0).then(std).otherwise(None)
        masks.append((z.abs() > threshold).fill_null(False))
    return pl.any_horizontal(masks) if len(masks) > 1 else masks[0]


def filter_zscore(
    df: pl.DataFrame,
    cols: list[str] | None = None,
    threshold: float = 20.0,
    *,
    date_col: str = "trade_date",
) -> pl.DataFrame:
    """按日截面 z-score 剔除异常收益行。

    Parameters
    ----------
    cols : 参与判定的收益列，默认所有 `fwd_ret*` 列。
    threshold : |z| 上限，默认 20。
    """
    if threshold <= 0:
        raise ValueError("threshold 必须为正数")
    cols = _resolve_cols(df, cols)
    return df.filter(~_bad_mask(cols, date_col, threshold))


def zscore_filter_stats(
    df: pl.DataFrame,
    cols: list[str] | None = None,
    threshold: float = 20.0,
    *,
    date_col: str = "trade_date",
) -> dict:
    """过滤前后的行数/占比，用于在报告里交代「样本被删掉多少」。

    删掉的行数不是零就应该显式展示 —— 阈值调得太小会把因子结论
    变成"剔除异常值后的结论"，读者必须知道这件事。
    """
    cols = _resolve_cols(df, cols)
    n_in = len(df)
    kept = filter_zscore(df, cols, threshold, date_col=date_col)
    n_out = len(kept)
    return {
        "threshold": float(threshold),
        "cols": cols,
        "n_in": n_in,
        "n_out": n_out,
        "n_dropped": n_in - n_out,
        "dropped_rate": (n_in - n_out) / n_in if n_in else 0.0,
    }
