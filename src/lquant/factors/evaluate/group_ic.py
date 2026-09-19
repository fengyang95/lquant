"""分组 IC：一个因子可能只是“小市值暴露”，分组 IC 一眼识破。"""
from __future__ import annotations

import polars as pl

from lquant.factors.evaluate.ic import ic_series

__all__ = ["ic_by_group", "size_group"]


def ic_by_group(df: pl.DataFrame, factor: str, ret_col: str, group_col: str, *,
                date_col: str = "trade_date", min_obs: int = 5) -> pl.DataFrame:
    """按组分别算 IC：组内相关不受其他组极值影响。

    用途：识破「因子收益全来自小市值组」。返回列：
    group, ic_mean, rank_ic_mean, ir, n_days。
    """
    if group_col not in df.columns:
        raise KeyError(f"分组列不存在: {group_col}")
    out = []
    for g_raw, sub in df.group_by(group_col):
        g = g_raw[0] if isinstance(g_raw, (list, tuple)) else g_raw
        if len(sub) < min_obs:
            continue
        # 组内每日样本数少（如 3 只一组的市值分组），同一 min_obs 透传给 ic_series
        s = ic_series(sub, factor, ret_col, date_col=date_col, min_obs=min_obs)
        if not len(s):
            continue
        ic, ric = s["ic"].mean(), s["rank_ic"].mean()
        ic_std = s["ic"].std()
        # 与 ic.py 的 STD_EPS 同口径：polars 常数序列 std 是 ~7e-18 伪零，
        # 不挡会输出 ±1e16 的荒谬 IR
        ir = ic / ic_std if ic_std is not None and ic_std > 1e-9 else float("nan")
        out.append({"group": g, "ic_mean": ic, "rank_ic_mean": ric,
                    "ir": ir,
                    "n_days": len(s)})
    return pl.DataFrame(out)


def size_group(df: pl.DataFrame, *, mcap_col: str = "amount", n_groups: int = 3,
               date_col: str = "trade_date", out: str = "size_q") -> pl.DataFrame:
    """按日截面把股票分成 n_groups 个市值组（amount 作代理），1 = 最小。

    追加 out 列，不改其余数据。
    """
    # 与 add_quantile 同款 NaN 防护：polars rank 会把 NaN 排到最大，
    # 不挡的话 NaN 全进最高市值组且 count() 分母被计入
    fin = pl.col(mcap_col).is_finite()
    cnt = fin.sum().over(date_col)
    q = (pl.when(fin)
         .then(pl.col(mcap_col).rank("ordinal").over(date_col) * n_groups / cnt)
         .otherwise(None)
         .ceil().clip(1, n_groups))
    return df.with_columns(q.cast(pl.Int32).alias(out))
