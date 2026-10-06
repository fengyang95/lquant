"""衰减分析：因子信号能撑多久。

调仓频率是被衰减曲线决定的，不是拍脑袋定的：
- IC 半衰期 3 天 → 必须高频调仓，否则收益被交易成本吃光
- IC 半衰期 40 天 → 月度调仓足够，高频只会白送手续费

半衰期用线性插值求 IC 降到峰值一半的持有期，
比「拟合指数曲线」稳（IC 序列噪声大，拟合经常不收敛）。
"""
from __future__ import annotations

import math

import polars as pl

from lquant.factors.evaluate.defaults import decay_horizons as _resolve_horizons
from lquant.factors.evaluate.ic import _summarize, ic_series
from lquant.factors.evaluate.returns import forward_return

__all__ = ["decay_profile", "half_life", "decay_summary", "suggest_rebalance"]


def decay_profile(df: pl.DataFrame, factor: str, horizons: list[int] | None = None,
                  *, price_col: str = "close", date_col: str = "trade_date",
                  symbol_col: str = "symbol") -> pl.DataFrame:
    """计算各持有期的 IC / RankIC / IR / t 值。

    会在内部补算前瞻收益，因此 df 里不必先有 fwd_ret_* 列。

    **已有的 `fwd_ret_{h}` 列一律直接复用，只补算缺失的持有期。**
    理由是「行过滤」：调用方（API 评价 / HTML 报告）常在
    filter_zscore、drop_nonfinite 剔掉若干行**之后**才调本函数，此时重算
    `forward_return` 的 ``shift(-h).over(symbol)`` 会跨过被剔掉的行 ——
    「h 个交易日后的收益」被静默算成跨越更长区间的收益（实测删一行后
    前一日收益从 0.0714 变成 0.1429），半衰期与调仓建议随之失真。
    复用调用方在行完整帧上算好的列，等于把这一口径固定下来。

    默认阶梯收口在 :mod:`lquant.factors.evaluate.defaults`（8 期，到 60 日）——
    此前这里写死一份、API 请求默认另一份、报告又一份，同一份报告换个入口
    就换一条曲线。
    """
    horizons = _resolve_horizons(horizons)
    missing = [h for h in horizons if f"fwd_ret_{h}" not in df.columns]
    d = (forward_return(df, price_col=price_col, periods=missing,
                        by=symbol_col, date_col=date_col) if missing else df)
    rows = []
    for h in horizons:
        col = f"fwd_ret_{h}"
        s = ic_series(d, factor, col, date_col=date_col)
        if not len(s):
            rows.append({"horizon": h, "ic": float("nan"), "rank_ic": float("nan"),
                         "ir": float("nan"), "t_stat": float("nan"), "n_days": 0})
            continue
        st = _summarize(s["ic"])
        st_r = _summarize(s["rank_ic"])
        rows.append({
            "horizon": h,
            "ic": st["mean"],
            "ic_std": st["std"],
            "ir": st["ir"],
            "t_stat": st["t_stat"],
            "rank_ic": st_r["mean"],
            "rank_ir": st_r["ir"],
            "positive_rate": st["positive_rate"],
            "n_days": st["n_days"],
        })
    return pl.DataFrame(rows)


def half_life(profile: pl.DataFrame, col: str = "ic") -> float:
    """IC 衰减到峰值一半所需的持有期（天），线性插值。

    峰值不是 h=1 —— 很多因子在第 2~5 天才最强（换手类因子尤其如此）。
    """
    if not len(profile):
        return float("nan")
    d = profile.select(["horizon", col]).drop_nulls().sort("horizon")
    if not len(d):
        return float("nan")
    hs = d["horizon"].to_list()
    vs = [abs(v) if v is not None else float("nan") for v in d[col].to_list()]
    peak = max((v for v in vs if math.isfinite(v)), default=float("nan"))
    if not math.isfinite(peak) or peak <= 0:
        return float("nan")
    target = peak / 2.0

    peak_i = max(range(len(vs)), key=lambda i: vs[i] if math.isfinite(vs[i]) else -1)
    for i in range(peak_i, len(vs) - 1):
        v0, v1 = vs[i], vs[i + 1]
        if not (math.isfinite(v0) and math.isfinite(v1)):
            continue
        if v1 <= target <= v0:
            h0, h1 = hs[i], hs[i + 1]
            if abs(v0 - v1) < 1e-15:
                return float(h0)
            return float(h0 + (v0 - target) / (v0 - v1) * (h1 - h0))
    return float(hs[-1])      # 到最长持有期都没衰减一半


def decay_summary(df: pl.DataFrame, factor: str, horizons: list[int] | None = None,
                  **kw) -> dict:
    """半衰期 + 建议调仓周期 + 各期 IC 表。"""
    prof = decay_profile(df, factor, horizons, **kw)
    hl = half_life(prof)
    return {
        "factor": factor,
        "half_life": hl,
        "suggested_rebalance": suggest_rebalance(hl),
        "profile": prof,
    }


def suggest_rebalance(half_life_days: float) -> str:
    """按半衰期给调仓建议：调仓周期 ≈ 半衰期，且不低于成本回收线。"""
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        return "unknown"
    if half_life_days <= 3:
        return "daily"
    if half_life_days <= 8:
        return "weekly"
    if half_life_days <= 15:
        return "biweekly"
    if half_life_days <= 45:
        return "monthly"
    return "quarterly"
