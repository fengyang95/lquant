"""Top-N 持仓收缩测试：把持仓向因子头部集中，看超额是否变厚、波动是否放大。

研报常规动作（分组回测之外）：取因子值最高的前 N 只等权持有，
对比 G_top 整组 —— 头部集中通常收益未必更高（甚至下降）、波动更大；
如果 Top50 比 Top100 明显差，说明头部收益靠的是「数量」而不是「强度」。

换手口径：每日 Top-N 成员与上一日的重合率 → turnover = 1 - overlap/N，
年化换手 = 日均换手 × 252（单边，未乘 2）。
"""
from __future__ import annotations

import polars as pl

from lquant.backtest.metrics import perf_from_returns
from lquant.factors.evaluate.excess import benchmark_series, excess_perf

__all__ = ["top_n_returns", "top_n_summary"]


def top_n_returns(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                  n: int = 100, *, date_col: str = "trade_date",
                  descending: bool = True) -> pl.DataFrame:
    """每期取因子值最大的前 n 只（等权），返回 (date, ret, members) 序列。

    members 列为当日持仓 symbol 列表 —— 供换手率计算复用，避免二次排序。
    """
    # NaN 不是 null：rank 会把 NaN 排到最大（descending 下优先入选 Top-N），
    # 必须连 NaN 一起剔（polars 实测 NaN rank=3 高于全部有限值）
    d = (df.select([date_col, "symbol", factor, ret_col])
         .filter(pl.col(factor).is_finite() & pl.col(ret_col).is_finite()))
    if not len(d):
        return pl.DataFrame(schema={"date": d[date_col].dtype, "ret": pl.Float64,
                                    "members": pl.List(pl.Utf8)})
    rank_col = "_rk"
    order = pl.col(factor).rank("ordinal", descending=descending).over(date_col)
    d = d.with_columns(order.alias(rank_col))
    picked = d.filter(pl.col(rank_col) <= n)
    return (
        picked.group_by(date_col).agg([
            pl.col(ret_col).mean().alias("ret"),
            pl.col("symbol").sort().alias("members"),
        ])
        .rename({date_col: "date"})
        .sort("date")
    )


def _member_turnover(members: list[list[str]]) -> float | None:
    """日均换手 = 1 - 相邻两日持仓重合率。少于 2 期无法计算，返回 None。"""
    if len(members) < 2:
        return None
    vals = []
    for prev, cur in zip(members[:-1], members[1:], strict=False):
        if not len(cur):
            continue
        overlap = len(set(cur) & set(prev))
        vals.append(1.0 - overlap / len(cur))
    return sum(vals) / len(vals) if vals else None


def top_n_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                  n_list: list[int] | None = None, *,
                  date_col: str = "trade_date", periods_per_year: int = 252,
                  bench: pl.DataFrame | None = None) -> pl.DataFrame:
    """每个 N 一行：绝对绩效 + 超额绩效 + 年化换手。

    返回列：n, annual_return, sharpe, max_drawdown, annual_excess,
            excess_sharpe, excess_mdd, annual_turnover, n_periods。
    """
    if n_list is None:
        n_list = [50, 100]
    if bench is None:
        bench = benchmark_series(df, ret_col, date_col=date_col)
    bench = bench.rename({date_col: "date"})
    rows = []
    for n in n_list:
        tr = top_n_returns(df, factor, ret_col, n, date_col=date_col)
        if not len(tr):
            continue
        j = tr.join(bench, on="date", how="inner").sort("date")
        if not len(j):
            continue
        r = j["ret"].to_numpy()
        p = perf_from_returns(r, periods_per_year=periods_per_year)
        pe = excess_perf(r, j["bench"].to_numpy(), periods_per_year=periods_per_year)
        to = _member_turnover(j["members"].to_list())
        rows.append({
            "n": n,
            "annual_return": p.get("annual_return", float("nan")),
            "sharpe": p.get("sharpe", float("nan")),
            "max_drawdown": p.get("max_drawdown", float("nan")),
            "annual_excess": pe.get("annual_return", float("nan")),
            "excess_sharpe": pe.get("sharpe", float("nan")),
            "excess_mdd": pe.get("max_drawdown", float("nan")),
            "annual_turnover": to * periods_per_year if to is not None else float("nan"),
            "n_periods": len(j),
        })
    return pl.DataFrame(rows)
