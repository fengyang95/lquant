"""IC / RankIC / IR 与显著性检验。

IC = 截面因子值与前瞻收益的相关系数。它是因子评价的第一指标：
- |IC| > 0.02 就算有效（A 股日频，别被 0.05 的回测骗了）
- IR = IC均值 / IC标准差，衡量稳定性，> 0.3 可用，> 0.5 很优秀
- t 检验判断 IC 是否显著不为 0：t = IR × √N，|t| > 2 才算数

只有 IC 均值没有 IR 和 t 值，等于没看 ——
一个均值 0.03 但标准差 0.15 的因子，实盘上是没法用的。
"""
from __future__ import annotations

import math

import polars as pl

__all__ = ["ic_series", "ic_summary", "ic_by_year", "ic_decay_table",
           "newey_west_tstat"]


def ic_series(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
              *, date_col: str = "trade_date", min_obs: int = 5) -> pl.DataFrame:
    """逐日截面 IC / RankIC。

    每天样本数少于 min_obs 时该日不计入（相关性在极小样本下没有意义）。
    """
    need = [factor, ret_col, date_col]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise KeyError(f"缺少列 {miss}")

    d = df.select([date_col, factor, ret_col]).with_columns(
        [pl.col(factor).cast(pl.Float64, strict=False),
         pl.col(ret_col).cast(pl.Float64, strict=False)]
    ).drop_nulls()

    out = (
        d.group_by(date_col)
        .agg([
            pl.len().alias("n"),
            pl.corr(factor, ret_col, method="pearson").alias("ic"),
            pl.corr(factor, ret_col, method="spearman").alias("rank_ic"),
        ])
        .filter(pl.col("n") >= min_obs)
        .sort(date_col)
    )
    return out


def _t_stat(mean: float, std: float, n: int) -> float:
    """t = mean / (std/√n)。std 为 0 或 n 过小时返回 nan。"""
    if n < 2 or std <= 0 or not math.isfinite(std):
        return float("nan")
    return mean / (std / math.sqrt(n))


def newey_west_tstat(x, lags: int | None = None) -> float:
    """NW 一致 t 值：日度 IC 强自相关下，朴素 t = IR·√N 会高估显著性 3~5 倍。"""
    s = pl.Series(x).drop_nulls() if not isinstance(x, pl.Series) else x.drop_nulls()
    n = len(s)
    if n < 2:
        return float("nan")
    lags = lags or int(4 * (n / 100) ** (2 / 9)) or 1
    a = s.to_numpy() - s.mean()
    s0 = float((a ** 2).sum()) / n
    lrv = s0
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)                     # Bartlett 核
        gamma_l = float((a[lag:] * a[:-lag]).sum()) / n
        lrv += 2.0 * w * gamma_l
    if lrv <= 0:
        return float("nan")
    se = math.sqrt(lrv / n)
    return float(s.mean()) / se


def _summarize(series: pl.Series, annualize: bool = True, *,
               nw_lags: int | None = None) -> dict:
    s = series.drop_nulls()
    n = len(s)
    if n == 0:
        return {"mean": float("nan"), "std": float("nan"), "ir": float("nan"),
                "t_stat": float("nan"), "t_stat_nw": float("nan"),
                "positive_rate": float("nan"), "skew": float("nan"), "n_days": 0}
    mean = float(s.mean())
    std = float(s.std()) or float("nan")
    ir = mean / std if std and std > 0 else float("nan")
    pos = float((s > 0).sum() / n)
    return {
        "mean": mean,
        "std": std,
        "ir": ir,
        "ir_annual": ir * math.sqrt(252) if annualize and math.isfinite(ir) else float("nan"),
        "t_stat": _t_stat(mean, std, n),
        "t_stat_nw": newey_west_tstat(s, lags=nw_lags),
        "positive_rate": pos,
        "skew": float(s.skew()) if n > 2 else float("nan"),
        "n_days": n,
    }


def ic_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
               *, method: str = "both", **kw) -> dict:
    """汇总：IC / RankIC 的均值、标准差、IR、t 值、正比例。

    method="pearson" 只算普通 IC，"spearman" 只算 RankIC（省一半计算），
    "both"（默认）都算，兼容旧行为。
    """
    if method not in ("pearson", "spearman", "both"):
        raise ValueError(f"method 必须是 pearson/spearman/both，得到: {method}")
    s = ic_series(df, factor, ret_col, **kw)
    out = {
        "factor": factor,
        "ret_col": ret_col,
        "method": method,
        "ic": _summarize(s["ic"]) if len(s) else {},
    }
    if method in ("spearman", "both"):
        out["rank_ic"] = _summarize(s["rank_ic"]) if len(s) else {}
    return out


def ic_by_year(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
               **kw) -> pl.DataFrame:
    """分年度 IC。因子失效往往不是慢慢变差，而是某一年突然反转。"""
    s = ic_series(df, factor, ret_col, **kw)
    if not len(s):
        return s
    return (
        s.with_columns(pl.col(kw.get("date_col", "trade_date")).dt.year().alias("year"))
        .group_by("year")
        .agg([
            pl.len().alias("n_days"),
            pl.col("ic").mean().alias("ic_mean"),
            pl.col("ic").std().alias("ic_std"),
            (pl.col("ic").mean() / pl.col("ic").std()).alias("ir"),
            (pl.col("ic") > 0).mean().alias("positive_rate"),
            pl.col("rank_ic").mean().alias("rank_ic_mean"),
        ])
        .sort("year")
    )


def ic_decay_table(df: pl.DataFrame, factor: str, ret_cols: list[str]) -> pl.DataFrame:
    """不同持有期的 IC 对照。交给 decay.py 做半衰期拟合。"""
    rows = []
    for rc in ret_cols:
        if rc not in df.columns:
            continue
        r = _summarize(ic_series(df, factor, rc)["ic"])
        rows.append({"ret_col": rc, **{k: v for k, v in r.items() if k != "n_days"}})
    return pl.DataFrame(rows)
