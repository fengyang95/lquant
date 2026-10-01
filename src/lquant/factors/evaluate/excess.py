"""超额收益体系：相对基准的年化超额 / 超额夏普 / 超额最大回撤 / 超额净值曲线。

口径（全文统一，别混）：
- 日度几何超额：r_ex = (1 + r) / (1 + b) - 1。复利一致，
  超额净值 nav_ex = nav_group / nav_bench，曲线终点就是总超额。
- 年化超额用几何年化（与 backtest.metrics._annualize 同口径），
  不用「组合年化 - 基准年化」的算术差（波动大时高估）。
- 超额夏普 = 年化超额 / 超额年化波动（对超额收益序列算）。

默认基准 = 股票池等权平均收益（截面均值），不依赖指数数据 ——
研究态先看「跑赢平均」；要对比沪深300 等指数基准时传入 bench_df。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.backtest.metrics import perf_from_returns

__all__ = [
    "benchmark_series",
    "excess_returns",
    "excess_perf",
    "group_excess_summary",
    "quantile_excess_nav",
]


def benchmark_series(df: pl.DataFrame, ret_col: str = "fwd_ret_1",
                     *, date_col: str = "trade_date") -> pl.DataFrame:
    """基准收益序列：每期截面等权平均。返回 (date, bench) 两列，已排序。"""
    return (
        df.select([date_col, ret_col])
        .drop_nulls()
        .group_by(date_col).agg(pl.col(ret_col).mean().alias("bench"))
        .sort(date_col)
    )


def excess_returns(rets, bench) -> np.ndarray:
    """日度几何超额 (1+r)/(1+b) - 1。等长数组，无效值（NaN）置 0 前先对齐剔除。"""
    r = np.asarray(rets, dtype=float)
    b = np.asarray(bench, dtype=float)
    if len(r) != len(b):
        raise ValueError(f"收益与基准长度不一致: {len(r)} vs {len(b)}")
    ok = np.isfinite(r) & np.isfinite(b) & (1.0 + b != 0)
    out = np.zeros(len(r), dtype=float)
    out[ok] = (1.0 + r[ok]) / (1.0 + b[ok]) - 1.0
    return out


def excess_perf(rets, bench, *, periods_per_year: int = 252) -> dict:
    """超额收益绩效：对几何超额序列跑 perf_from_returns（含年化超额/超额夏普/超额回撤）。"""
    ex = excess_returns(rets, bench)
    return perf_from_returns(ex, periods_per_year=periods_per_year)


def group_excess_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                         n_groups: int = 10, bench: pl.DataFrame | None = None, *,
                         date_col: str = "trade_date",
                         periods_per_year: int = 252) -> pl.DataFrame:
    """每组相对基准的超额绩效表。

    返回列：q, annual_return, annual_excess, excess_sharpe, excess_mdd, n_periods。
    bench 为 None 时用股票池等权基准。
    """
    from lquant.factors.evaluate.quantile import group_returns

    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    if not len(g):
        return pl.DataFrame(schema={"q": pl.Int32, "annual_return": pl.Float64,
                                    "annual_excess": pl.Float64, "excess_sharpe": pl.Float64,
                                    "excess_mdd": pl.Float64, "n_periods": pl.UInt32})
    if bench is None:
        bench = benchmark_series(df, ret_col, date_col=date_col)
    g = g.join(bench, on=date_col, how="inner").sort([date_col, "q"])
    rows = []
    for q in range(1, n_groups + 1):
        sub = g.filter(pl.col("q") == q)
        if not len(sub):
            continue
        p = excess_perf(sub["ret"].to_numpy(), sub["bench"].to_numpy(),
                        periods_per_year=periods_per_year)
        rows.append({
            "q": q,
            "annual_return": p.get("annual_return", float("nan")),
            "annual_excess": p.get("annual_return", float("nan")),
            "excess_sharpe": p.get("sharpe", float("nan")),
            "excess_mdd": p.get("max_drawdown", float("nan")),
            "n_periods": p.get("n_periods", 0),
        })
    # annual_return 与 annual_excess 在几何超额口径下同义（超额序列的年化即年化超额）
    return pl.DataFrame(rows)


def quantile_excess_nav(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1",
                        n_groups: int = 10, bench: pl.DataFrame | None = None, *,
                        date_col: str = "trade_date") -> pl.DataFrame:
    """各组超额净值曲线（组净值 / 基准净值）。宽表：date + ex_q1..ex_qN + ex_long_short。

    ex_long_short = ex_qN / ex_q1 —— 多空两端的相对强弱，比绝对多空更贴近
    「多头组合能否跑赢空头组合」的实盘直觉。
    """
    from lquant.factors.evaluate.quantile import group_returns, pivot_group_returns

    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    if not len(g):
        return g
    if bench is None:
        bench = benchmark_series(df, ret_col, date_col=date_col)
    piv = pivot_group_returns(g, n_groups, date_col=date_col)
    piv = piv.join(bench, on=date_col, how="inner").sort(date_col)

    bn = (pl.col("bench").fill_null(0.0) + 1.0).cum_prod()
    exprs = []
    for q in range(1, n_groups + 1):
        # 空分位组（该组某日无成员）→ 整列记 null，不毒化曲线（与旧实现口径一致）
        exprs.append(
            pl.when(pl.col(str(q)).is_null().any())
            .then(pl.lit(None, dtype=pl.Float64))
            .otherwise(_cum_nav_col(str(q)) / bn)
            .alias(f"ex_q{q}")
        )
    out = piv.with_columns(exprs).select(
        [date_col] + [f"ex_q{q}" for q in range(1, n_groups + 1)]
    )
    return out.with_columns(
        (pl.col(f"ex_q{n_groups}") / pl.col("ex_q1")).alias("ex_long_short")
    )


def _cum_nav_col(col: str) -> pl.Expr:
    return (pl.col(col).fill_null(0.0) + 1.0).cum_prod()
